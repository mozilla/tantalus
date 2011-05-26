# ***** BEGIN LICENSE BLOCK *****
# Version: MPL 1.1/GPL 2.0/LGPL 2.1
#
# The contents of this file are subject to the Mozilla Public License Version
# 1.1 (the "License"); you may not use this file except in compliance with
# the License. You may obtain a copy of the License at
# http://www.mozilla.org/MPL/
#
# Software distributed under the License is distributed on an "AS IS" basis,
# WITHOUT WARRANTY OF ANY KIND, either express or implied. See the License
# for the specific language governing rights and limitations under the
# License.
#
# The Original Code is Mozilla Crash Automation Testing.
#
# The Initial Developer of the Original Code is
# Mozilla Corporation.
# Portions created by the Initial Developer are Copyright (C) 2010
# the Initial Developer. All Rights Reserved.
#
# Contributor(s):
# Bob Clary
#
# Alternatively, the contents of this file may be used under the terms of
# either the GNU General Public License Version 2 or later (the "GPL"), or
# the GNU Lesser General Public License Version 2.1 or later (the "LGPL"),
# in which case the provisions of the GPL or the LGPL are applicable instead
# of those above. If you wish to allow use of your version of this file only
# under the terms of either the GPL or the LGPL, and not to allow others to
# use your version of this file under the terms of the MPL, indicate your
# decision by deleting the provisions above and replace them with the notice
# and other provisions required by the GPL or the LGPL. If you do not delete
# the provisions above, a recipient may use your version of this file under
# the terms of any one of the MPL, the GPL or the LGPL.
#
# ***** END LICENSE BLOCK *****

from optparse import OptionParser
import os
import stat
import time
import datetime
import sys
import subprocess
import re
import base64 # for encoding document attachments.
import urllib
import glob
import signal
import tempfile

sisyphus_dir     = os.environ["TEST_DIR"]
tempdir          = os.path.join(sisyphus_dir, 'python')
if tempdir not in sys.path:
    sys.path.append(tempdir)

tempdir          = os.path.join(tempdir, 'sisyphus')
if tempdir not in sys.path:
    sys.path.append(tempdir)

tempdir          = os.path.join(tempdir, 'webapp')
if tempdir not in sys.path:
    sys.path.append(tempdir)

os.environ['DJANGO_SETTINGS_MODULE'] = 'sisyphus.webapp.settings'

import sisyphus.webapp.settings
sisyphus_url      = os.environ["SISYPHUS_URL"]
post_files_url    = sisyphus_url + '/post_files/'

from sisyphus.webapp.bughunter import models
from sisyphus.automation import utils, worker, program_info

os.environ["XPCOM_DEBUG_BREAK"]="stack"

class UnitTestWorker(worker.Worker):

    def __init__(self, options):
        worker.Worker.__init__(self, "unittest", options)

        self.debugger             = options.debugger
        self.debugger_args        = options.debugger_args
        self.global_timeout       = options.global_timeout
        self.test_timeout         = options.test_timeout
        self.all_test_results     = options.all_test_results
        self.debug                = options.debug
        self.model_test_run       = models.UnitTestRun
        self.model_test_assertion = models.UnitTestAssertion
        self.model_test_crash     = models.UnitTestCrash
        self.model_test_valgrind  = models.UnitTestValgrind
        self.model_test_crash_dump_meta_data = models.UnitTestCrashDumpMetaData

        self.testrun_row = None
        self.save()

    def runTest(self, extra_test_args):

        # kill any test processes still running.
        self.killTest()

        timestamp = utils.getTimestamp()

        # The test frameworks do not bracket test results within begin/end
        # blocks (with the exception of the jstests). Instead they run the
        # test and then emit a test result line after the test has
        # completed. Therefore we collect information about a test until
        # we get a match for a test result then emit the appropriate
        # result documents for assertions, valgrind messages, test
        # results. Typically crashes will occur before the test result is
        # emitted in the log, so we instead will list the test immediately
        # prior to the crashing test.

        executablepath   = None
        profilename      = ""
        reExecutablePath = re.compile(r'^environment: executablepath=(.*)')
        reProfileName    = re.compile(r'^environment: profilename=(.*)')
        reAssertionFail  = re.compile(r'^(Assertion failure: .*), at .*')
        reABORT          = re.compile(r'^.?###\!\!\! (ABORT: .*), file (.*), line [0-9]+.*')
        reASSERTION      = re.compile(r'^.?###\!\!\! ASSERTION: (.*), file (.*), line [0-9]+.*')
        reValgrindLeader = re.compile(r'^==[0-9]+==')
        # reftest
        # REFTEST TEST-START | testid
        #       action: process previously collected messages
        #               for previous testid, set current testid.
        # REFTEST TEST-PASS | testid | message
        # REFTEST TEST-KNOWN-FAIL | testid | message
        #       action: If all results selected, output unittest result.
        # REFTEST TEST-UNEXPECTED-FAIL | testid | message
        #       action: Output unittest result.
        reReftestStart   = re.compile(r'REFTEST TEST-START \| (.*)')
        reReftestResult  = re.compile(r'REFTEST TEST-(.*?) \| (.*?) \| (.*)')
        # mochitest
        # 9999 INFO TEST-START | testid...
        #       action: process previously collected messages
        #               for previous testid, set current testid.
        # 9999 INFO TEST-PASS | testid | message
        # 9999 INFO TEST-KNOWN-FAIL | testid | message
        #       action: If all results selected, output unittest result.
        # 9999 ERROR TEST-UNEXPECTED-FAIL | testid | message
        #       action: Output unittest result.
        reMochitestStart = re.compile(r'[0-9]+ INFO TEST-START \| (.*)')
        reMochitestInfo = re.compile(r'[0-9]+ INFO TEST-INFO')
        reMochitestResultPass = re.compile(r'[0-9]+ INFO TEST-(.*?) \| (.*?) \| (.*)')
        reMochitestResultError = re.compile(r'[0-9]+ ERROR TEST-(.*?) \| (.*?) \| (.*)')
        reMochitestEnd = re.compile(r'[0-9]+ INFO TEST-END \| (.*)')
        # xpctest
        # TEST_PASS | testid | message
        # TEST-KNOWN-FAIL | testid | message # this may not be valid.
        #       action: process previously collected messages 
        #               for this testid.
        # TEST-UNEXPECTED-FAIL | testid | message
        #               includes overall test run message not related to xpctests.
        #       action: process previously collected messages
        #               for this testid.
        reXpctestStart  = re.compile(r'TEST-(.*?) \| (.*?) \| running test')
        reXpctestResult  = re.compile(r'TEST-(.*?) \| (.*?) \| (.*)')

        # buffers to hold assertions and valgrind messages until
        # a test result is seen in the output.
        assertion_dict = {}
        valgrind_text  = ""

        size    = 0         # current log size
        maxsize = 0xfffffff # maximum log size ~ 268435455
        data    = u""       # log buffer

        # update the worker every hour to keep from being marked a zombie.
        update_interval = datetime.timedelta(minutes=60)
        last_update_time = datetime.datetime.now() - 2*update_interval

        # time out the unittest after unittest_timeout seconds
        unittest_timeout   = datetime.timedelta(seconds = self.global_timeout * 3600)
        unittest_starttime = datetime.datetime.now()
        unittest_endtime   = unittest_starttime + unittest_timeout

        # XXX: using set-build-env.sh is a kludge needed on windows which
        # allows us to use cygwin to setup the msys mozilla-build
        # environment and call make using msys.  this is a problem in that
        # the processes become disconnected and the msys processes are
        # orphans with respect to cygwin.  killing the process created by
        # Popen will not kill the msys processes or firefox processes
        # created during the make.

        # The log filename does not include the timezone offset as the other sisyphus test logs do.
        logfilename = "%s/results/%s,%s,%s,%s,%s,%s,%s.log" % (sisyphus_dir,
                                                               datetime.datetime.now().strftime('%Y-%m-%d-%H-%M-%S'),
                                                               self.product,
                                                               self.branch,
                                                               self.buildtype,
                                                               self.os_id,
                                                               self.hostname,
                                                               self.testrun_row.unittestbranch.test)


        logfile = open(logfilename, 'wb+')

        proc = subprocess.Popen(
            [
                "./bin/set-build-env.sh",
                "-p", self.product,
                "-b", self.branch,
                "-T", self.buildtype,
                "-c", "make -C firefox-%s EXTRA_TEST_ARGS=\"%s\" %s" % (self.buildtype, extra_test_args, self.testrun_row.unittestbranch.test)
                ],
            bufsize=1, # line buffered
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            close_fds=True)

        unittest_id = 'startup'

        try:
            # initial read timeout is the global timeout for the unittest
            line = utils.timedReadLine(proc.stdout, unittest_timeout.seconds)
            line = utils.makeUnicodeString(line)

            while line:

                current_time = datetime.datetime.now()

                if current_time - last_update_time > update_interval:
                    self.datetime = utils.getTimestamp()
                    self.save()
                    last_update_time = datetime.datetime.now()

                if current_time - unittest_starttime > unittest_timeout:
                    raise Exception('UnitTestTimeout')

                if size > maxsize:
                    raise Exception('UnitTestSizeError')

                size += len(line)

                logfile.write(line.encode('utf-8'))

                if not executablepath:
                    match = reExecutablePath.match(line)
                    if match:
                        executablepath = match.group(1)

                if not profilename:
                    match = reProfileName.match(line)
                    if match:
                        profilename = match.group(1)

                next_unittest_id = None
                unittest_result = 'Unknown'
                unittest_message = None
                # process_messages controls when we dump assertions and valgrinds that
                # we have collected so far. Dump them whenever we see a new test.
                process_messages = False

                match = reReftestStart.match(line)
                if match:
                    process_messages = True
                    next_unittest_id = match.group(1)
                else:
                    # reReftestResult also matches reReftestStart lines so must be tested
                    # only after reRefTestStart has been excluded.
                    match = reReftestResult.match(line)
                    if match:
                        unittest_result = match.group(1)
                        unittest_id = match.group(2)
                        unittest_message = match.group(3)
                    else:
                        match = reMochitestInfo.match(line)
                        if match:
                            pass # ignore TEST-INFO lines. Need to test first since otherwise
                                 # would test results would match test info lines.
                        else:
                            match = reMochitestStart.match(line)
                            if match:
                                process_messages = True
                                next_unittest_id = match.group(1)
                            else:
                                match = reMochitestEnd.match(line)
                                if match:
                                    pass # ignore TEST-END lines.
                                else:
                                    match = reMochitestResultPass.match(line)
                                    if match:
                                        unittest_result = match.group(1)
                                        unittest_id = match.group(2)
                                        unittest_message = match.group(3)
                                    else:
                                        match = reMochitestResultError.match(line)
                                        if match:
                                            unittest_result = match.group(1)
                                            unittest_id = match.group(2)
                                            unittest_message = match.group(3)
                                        else:
                                            match = reXpctestStart.match(line)
                                            if match:
                                                process_messages = True
                                                next_unittest_id = match.group(2)
                                            else:
                                                match = reXpctestResult.match(line)
                                                if match:
                                                    process_messages = True
                                                    unittest_result = match.group(1)
                                                    unittest_id = match.group(2)
                                                    unittest_message = match.group(3)

                if process_messages:
                    self.process_assertions(timestamp, assertion_dict, unittest_id, self.testrun_row.unittestbranch.test, extra_test_args)
                    self.process_valgrind(timestamp, valgrind_text, unittest_id, self.testrun_row.unittestbranch.test,  extra_test_args)
                    assertion_dist   = {}
                    valgrind_text    = ""
                    unittest_id = next_unittest_id

                if (unittest_id and unittest_result != 'Unknown' and
                    (self.all_test_results or re.search('UNEXPECTED', unittest_result))):
                    # by default, only output unittest results if they are
                    # unexpected
                    unittestresult = models.UnitTestResult(
                        testrun = self.testrun_row,
                        unittest_id = unittest_id,
                        unittest_result = unittest_result,
                        unittest_message = unittest_message,
                        )
                    unittestresult.save()

                match = reASSERTION.match(line)
                if match:
                    # record the assertion for later output when we know the test
                    assertionmessage = match.group(1)
                    assertionfile    = re.sub('^([a-zA-Z]:/|/[a-zA-Z]/)', '/', re.sub(r'\\', '/', match.group(2)))
                    assertionkey     = assertionmessage + ':' + assertionfile
                    if assertionkey in assertion_dict:
                        assertion_dict[assertionkey]["count"] += 1
                    else:
                        assertion_dict[assertionkey] = {
                            "message": assertionmessage,
                            "file"   : assertionfile,
                            "stack"  : "", # need to collect stack
                            "count"  : 1
                            }

                match = reValgrindLeader.match(line)
                if match:
                    valgrind_text += line

                match = reAssertionFail.match(line)
                if match:
                    self.testrun_row.fatal_message = match.group(1)

                match = reABORT.match(line)
                if match:
                    self.testrun_row.fatal_message = match.group(1)

                # subsequent read timeout is the remaining time before the
                # global timeout for the unittest.
                read_timeout = (unittest_endtime - current_time).seconds
                line = utils.timedReadLine(proc.stdout, read_timeout)
                line = utils.makeUnicodeString(line)

            try:
                def timedProcCommunicate_handler(signum, frame):
                    raise Exception('ProcCommunicateTimeout')

                signal.signal(signal.SIGALRM, timedProcCommunicate_handler)
                signal.alarm(read_timeout)
                proc.communicate()
                signal.alarm(0)
            except:
                exceptionType, exceptionValue, errorMessage = utils.formatException()
                self.logMessage("runTest: %s %s %s: exception: %s" %
                                (self.product, self.branch, self.testrun_row.unittestbranch.test, errorMessage))

        except:
            exceptionType, exceptionValue, errorMessage = utils.formatException()
            if proc.poll() is None:
                self.logMessage("runTest: %s %s %s: exception %s." %
                                (self.product, self.branch, self.testrun_row.unittestbranch.test, errorMessage))

            if exceptionType == KeyboardInterrupt:
                raise

        if proc.poll() is None:
            self.logMessage("runTest: %s %s %s: process not terminated cleanly. killing process." %
                            (self.product, self.branch, self.testrun_row.unittestbranch.test))
            self.killTest()

        self.testrun_row.returncode = proc.poll()
        self.testrun_row.exitstatus += utils.convertReturnCodeToExitStatusMessage(proc.returncode)

        if proc.returncode == -2:
            raise KeyboardInterrupt

        logfile.close()

        baselogfilename = os.path.basename(logfilename)
        loguploadpath = 'logs/' + baselogfilename[:13] # CCYY-MM-DD-HH
        dmpuploadpath = 'minidumps/' + baselogfilename[:13] # CCYY-MM-DD-HH
        uploader = utils.FileUploader(post_files_url,
                                      self.model_test_run.__name__, self.testrun_row, self.testrun_row.id,
                                      loguploadpath)
        uploader.add('log', baselogfilename, logfilename, True)
        self.testrun_row = uploader.send()

        symbolsPath = os.path.join(executablepath, 'crashreporter-symbols')

        self.process_dump_files(timestamp, profilename, unittest_id, symbolsPath, dmploadpath)

        self.testrun_row.save()

        # process any valgrind messages not associated with a test.
        self.process_assertions(timestamp, assertion_dict, "shutdown", self.testrun_row.unittestbranch.test, extra_test_args)
        self.process_valgrind(timestamp, valgrind_text, "shutdown", self.testrun_row.unittestbranch.test, extra_test_args)

    def getJob(self):

        """
        return a test job for this worker
        matching os_name, cpu_name, os_version
        """
        locktimeout     = 300

        if not utils.getLock('sisyphus.bughunter.unittestrun', locktimeout):
            self.debugMessage("getJob: lock timed out")
        else:
            try:
                unittestrun_row = models.UnitTestRun.objects.filter(state__exact = "waiting",
                                                                    os_name__exact = self.os_name,
                                                                    os_version__exact = self.os_version,
                                                                    cpu_name__exact = self.build_cpu_name)[0]
                unittestrun_row.worker = self.worker_row
                unittestrun_row.state = 'executing'
                unittestrun_row.build_cpu_name = self.build_cpu_name
                unittestrun_row.changeset = self.build_row.changeset
                unittestrun_row.save()

            except IndexError:
                unittestrun_row = None
                pass

            except models.UnitTestRun.DoesNotExist:
                unittestrun_row = None
                pass

            finally:
                lockDuration = utils.releaseLock('sisyphus.bughunter.unittestrun')
                if lockDuration > datetime.timedelta(seconds=5):
                    self.logMessage("getJobs: releaseLock('sisyphus.bughunter.unittestrun') duration: %s" % lockDuration)

        return unittestrun_row

    def createJobs(self):

        if "EXTRA_TEST_ARGS" in os.environ:
            extra_test_args = os.environ["EXTRA_TEST_ARGS"] + " "
        else:
            extra_test_args = ""

        if self.debugger:
             extra_test_args += "--debugger='" + self.debugger + "' "
             if self.debugger_args:
                  extra_test_args += "--debugger-args='" + self.debugger_args + "' "

        if self.test_timeout:
             extra_test_args += "--timeout=" + str(self.test_timeout) + " "

        unittestbranch_rows = models.UnitTestBranch.objects.all()

        for unittestbranch_row in unittestbranch_rows:
            # special case chunking mochitest-plain
            if unittestbranch_row.test != "mochitest-plain":
                unittestrun = models.UnitTestRun(
                    os_name         = self.os_name,
                    os_version      = self.os_version,
                    cpu_name        = self.cpu_name,
                    product         = self.product,
                    branch          = self.branch,
                    buildtype       = self.buildtype,
                    build_cpu_name  = None,
                    worker          = None,
                    unittestbranch  = unittestbranch_row,
                    changeset       = None,
                    datetime        = utils.getTimestamp(),
                    major_version   = None, # XXX remove major version?
                    crashed         = False,
                    extra_test_args = extra_test_args, # XXX
                    exitstatus      = '',
                    log             = None,
                    state           = 'waiting',
                    )
                unittestrun.save()
            else:
                total_chunks = 10
                for chunk in range(total_chunks):
                    chunk_options = '%s --total-chunks=%d --this-chunk=%d' % (extra_test_args, total_chunks, chunk+1)
                    unittestrun = models.UnitTestRun(
                        os_name         = self.os_name,
                        os_version      = self.os_version,
                        cpu_name        = self.cpu_name,
                        product         = self.product,
                        branch          = self.branch,
                        buildtype       = self.buildtype,
                        build_cpu_name  = self.cpu_name,
                        worker          = None,
                        unittestbranch  = unittestbranch_row,
                        changeset       = None,
                        datetime        = utils.getTimestamp(),
                        major_version   = None, # XXX remove major version?
                        crashed         = False,
                        extra_test_args = chunk_options, # XXX
                        exitstatus      = '',
                        log             = None,
                        state           = 'waiting',
                        )
                    unittestrun.save()

    def doWork(self):

        waittime  = 0

        build_checkup_interval = datetime.timedelta(hours=3)
        checkup_interval       = datetime.timedelta(minutes=5)
        last_checkup_time      = datetime.datetime.now() - 2*checkup_interval

        while True:

            if datetime.datetime.now() - last_checkup_time > checkup_interval:
                self.checkForUpdate()
                self.killZombies()
                # XXXself.freeOrphanJobs()
                last_checkup_time = datetime.datetime.now()

            sys.stdout.flush()
            time.sleep(waittime)
            waittime = 0

            self.testrun_row = self.getJob()
            if not self.testrun_row:
                if self.state != "waiting":
                    self.logMessage('Creating new jobs.')
                major_version = None
                branch_data   = None
                branch        = None
                waittime      = 0
                self.state    = "waiting"
                self.datetime = utils.getTimestamp()
                self.save()
                self.createJobs()
                continue

            major_version  = self.testrun_row.major_version
            self.product   = self.testrun_row.product
            self.branch    = self.testrun_row.branch
            self.buildtype = self.testrun_row.buildtype

            build_needed = self.isNewBuildNeeded(build_checkup_interval)

            if build_needed:
                if self.isBuilder:
                    self.publishNewBuild()
                else:
                    self.installBuild()

                if not self.build_date:
                    self.testrun_row.worker  = None
                    self.testrun_row.state   = 'waiting'
                    self.testrun_row.save()
                    self.state           = 'waiting'
                    self.datetime        = utils.getTimestamp()
                    self.testrun_row = None
                    self.save()
                    waittime = 300
                    continue

            if self.state == "waiting":
                self.logMessage('New tests available to process, going active.')

            try:
                self.runTest(self.testrun_row.extra_test_args)
            except KeyboardInterrupt, SystemExit:
                raise
            except:
                exceptionType, exceptionValue, errorMessage = utils.formatException()
                if str(exceptionValue) == 'UnitTestWorker.runTest.FatalError':
                    raise
                self.logMessage("doWork: error %s in runTest. %s, exception: %s" %
                                (exceptionValue, self.testrun_row, errorMessage))
            finally:
                if self.testrun_row:
                    self.testrun_row.state = 'completed'
                    self.testrun_row.save()
                self.state            = 'completed'
                self.testrun_row  = None
                self.save()

program_info.init(globals())

def main():
    global this_worker

    usage = '''usage: %prog [options]

Example:
%prog
'''
    parser = OptionParser(usage=usage)

    parser.add_option('--build', action='store_true',
                      default=False, help='Perform own builds')

    parser.add_option('--debugger', action='store', type='string',
                      dest='debugger',
                      default=None,
                      help='Add --debugger=value to environment variable EXTRA_TEST_ARGS ' +
                      'Defaults to None.')

    parser.add_option('--debugger-args', action='store', type='string',
                      dest='debugger_args',
                      default=None,
                      help='Add --debugger-args=value to environment variable EXTRA_TEST_ARGS. ' +
                      'Defaults to None.')

    parser.add_option('--global-timeout', action='store', type='int',
                      dest='global_timeout',
                      default=3,
                      help='Terminate the test if it runs longer than value hours. ' +
                      'Defaults to 3.')

    parser.add_option('--test-timeout', action='store', type='int',
                      dest='test_timeout',
                      default=None,
                      help='Add --timeout=value to environment variable EXTRA_TEST_ARGS. ' +
                      'This is the per test timeout in seconds for the tests. ' +
                      'Defaults to None.')

    parser.add_option('--all-test-results', action='store_true',
                      dest='all_test_results',
                      help='By default only record unexpected unittest results. ' +
                      'Add --all-test-results to record all results.')

    parser.add_option('--debug', action='store_true',
                      dest='debug',
                      default=False,
                      help='turn on debug messages')

    (options, args) = parser.parse_args()

    if options.debugger_args and not options.debugger:
         parser.print_help()
         exit(1)

    exception_counter = 0

    this_worker     = UnitTestWorker(options)

    this_worker.logMessage('starting worker %s %s %s with program dated %s' %
                          (this_worker.os_name, this_worker.os_version, this_worker.cpu_name,
                           time.ctime(program_info.programModTime)))
    while True:
        try:
            this_worker.doWork()
        except KeyboardInterrupt, SystemExit:
            raise
        except:

            if this_worker.testrun_row:
                this_worker.testrun_row.state = 'waiting'
                this_worker.testrun_row.worker = None
                this_worker.testrun_row.save()
                this_worker.testrun_row = None
                this_worker.save()

            exception_counter += 1
            if exception_counter > 100:
                print "Too many errors. Terminating."
                sys.exit(2)

            exceptionType, exceptionValue, errorMessage = utils.formatException()

            if str(exceptionValue) == 'UnitTestWorker.runTest.FatalError':
                raise

            if str(exceptionValue) == 'WorkerInconsistent':
                # If we were disabled, sleep for 5 minutes and check our state again.
                # otherwise restart.
                if this_worker.state == "disabled":
                    while True:
                        time.sleep(300)
                        curr_worker_doc = models.Worker.objects.get(pk = self.hostname)
                        if curr_worker_doc.state != "disabled":
                            this_worker.state = "waiting"
                            break

            this_worker.logMessage('main: exception %s: %s' % (str(exceptionValue), errorMessage))

            time.sleep(60)


if __name__ == "__main__":
    try:
        this_worker = None
        restart = True
        main()
    except KeyboardInterrupt, SystemExit:
        restart = False
    except:
        exceptionType, exceptionValue, errorMessage = utils.formatException()
        if str(exceptionValue) not in "0,NormalExit":
            print ('main: exception %s: %s' % (str(exceptionValue), errorMessage))

    # kill any test processes still running.
    if this_worker:
        this_worker.killTest()

    if this_worker is None:
        exit(2)

    if restart:
        # continue trying to log message until it succeeds.
        this_worker.logMessage('Program restarting')
        this_worker.reloadProgram()
    else:
        this_worker.logMessage('Program terminating')
        this_worker.state = 'dead'
        this_worker.save()
