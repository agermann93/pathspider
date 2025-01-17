"""
Qofspider: Framework for coordinating active measurements on large target lists
with system-level network stack state (sysctls, iptables rules, etc.) and flow
records generated by using QoF to observe the interactions.

.. moduleauthor:: Brian Trammell <brian@trammell.ch>

Derived and generalized from ECN Spider
(c) 2014 Damiano Boppart <hat.guy.repo@gmail.com>

    This program is free software; you can redistribute it and/or modify
    it under the terms of the GNU General Public License as published by
    the Free Software Foundation; either version 2 of the License, or
    (at your option) any later version.

    This program is distributed in the hope that it will be useful,
    but WITHOUT ANY WARRANTY; without even the implied warranty of
    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
    GNU General Public License for more details.

    You should have received a copy of the GNU General Public License along
    with this program; if not, write to the Free Software Foundation, Inc.,
    51 Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA.

"""

from collections import namedtuple
from ipaddress import ip_address
import tempfile
import subprocess
import threading
import socketserver
import queue
import ipfix.reader
import ipfix
import yaml
import sys
import os
import time
import logging
import socket

###
### Utility Classes
###

class SemaphoreN(threading.BoundedSemaphore):
    """
    An extension to the standard library's BoundedSemaphore that provides functions to handle n tokens at once.
    """
    def __init__(self, value):
        self._VALUE = value
        super().__init__(self._VALUE)
        self.empty()

    def __str__(self):
        return 'SemaphoreN with a maximum value of {}.'.format(self._VALUE)

    def acquire_n(self, value=1, blocking=True, timeout=None):
        """
        Acquire ``value`` number of tokens at once.

        The parameters ``blocking`` and ``timeout`` have the same semantics as :class:`BoundedSemaphore`.

        :returns: The same value as the last call to `BoundedSemaphore`'s :meth:`acquire` if :meth:`acquire` were called ``value`` times instead of the call to this method.
        """
        ret = None
        for _ in range(value):
            ret = self.acquire(blocking=blocking, timeout=timeout)
        return ret

    def release_n(self, value=1):
        """
        Release ``value`` number of tokens at once.

        :returns: The same value as the last call to `BoundedSemaphore`'s :meth:`release` if :meth:`release` were called ``value`` times instead of the call to this method.
        """
        ret = None
        for _ in range(value):
            ret = self.release()
        return ret

    def empty(self):
        """
        Acquire all tokens of the semaphore.
        """
        while self.acquire(blocking=False):
            pass

QUEUE_SIZE = 1000
QUEUE_SLEEP = 0.5

QOF_INITIAL_SLEEP = 3
QOF_FINAL_SLEEP = 3

class QofSpider:
    """
    A spider consists of a configurator (which alternates between two
    system configurations), a large number of workers (for performing some
    network action for each configuration), an IPFIX collector wrapped
    around QoF, and a thread that merges results from the workers
    with flow records from the collector.

    """

    def __init__(self, worker_count, interface_uri, qof_port=4739, check_interrupt=None):
        self.running = False
        self.stopping = False
        self.terminating = False

        self.check_interrupt = check_interrupt

        self.worker_count = worker_count
        self.interface_uri = interface_uri
        self.qof_port = qof_port

        self.sem_config_zero = SemaphoreN(worker_count)
        self.sem_config_zero.empty()
        self.sem_config_zero_rdy = SemaphoreN(worker_count)
        self.sem_config_zero_rdy.empty()
        self.sem_config_one = SemaphoreN(worker_count)
        self.sem_config_one.empty()
        self.sem_config_one_rdy = SemaphoreN(worker_count)
        self.sem_config_one_rdy.empty()

        self.jobqueue = queue.Queue()
        self.flowqueue = queue.Queue(QUEUE_SIZE)
        self.resqueue =  queue.Queue(QUEUE_SIZE)

        self.restab = {}
        self.flowtab = {}

        self.listener = None
        self.qofproc = None

        self.qofowner_thread = None
        self.worker_threads = []
        self.qoflistener_thread = None
        self.configurator_thread = None
        self.interrupter_thread = None
        self.merger_thread = None

        self.lock = threading.Lock()
        self.exception = None

    def configurator(self):
        """
        Thread which synchronizes on a set of semaphores and alternates
        between two system states.
        """
        logger = logging.getLogger('qofspider')

        while self.running:
            logger.debug("setting config zero")
            self.config_zero()
            logger.debug("config zero active")
            self.sem_config_zero.release_n(self.worker_count)
            self.sem_config_one_rdy.acquire_n(self.worker_count)
            logger.debug("setting config one")
            self.config_one()
            logger.debug("config one active")
            self.sem_config_one.release_n(self.worker_count)
            self.sem_config_zero_rdy.acquire_n(self.worker_count)

        # In case the master exits the run loop before all workers have,
        # these tokens will allow all workers to run through again,
        # until the next check at the start of the loop
        self.sem_config_zero.release_n(self.worker_count)
        self.sem_config_one.release_n(self.worker_count)

    def config_zero(self):
        raise NotImplementedError("Cannot instantiate an abstract Qofspider")

    def config_one(self):
        raise NotImplementedError("Cannot instantiate an abstract Qofspider")

    def interrupter(self):
        if self.check_interrupt is None:
            return

        logger = logging.getLogger('qofspider')
        while self.running:
            if self.check_interrupt():
                logger.warn("qofspider is being interrupted")
                logger.warn("trying to abort %d jobs", self.jobqueue.qsize())
                while not self.jobqueue.empty():
                    self.jobqueue.get()
                    self.jobqueue.task_done()
                self.stop()
                break
            time.sleep(5)

    def worker(self):
        logger = logging.getLogger('qofspider')

        while self.running:
            try:
                job = self.jobqueue.get_nowait()
                logger.debug("got a job: "+repr(job))
            except queue.Empty:
                #logger.debug("no job available, sleeping")
                # spin the semaphores
                self.sem_config_zero.acquire()
                time.sleep(QUEUE_SLEEP)
                self.sem_config_one_rdy.release()
                self.sem_config_one.acquire()
                time.sleep(QUEUE_SLEEP)
                self.sem_config_zero_rdy.release()
            else:
                # Hook for preconnection
                pcs = self.pre_connect(job)

                # Wait for configuration zero
                self.sem_config_zero.acquire()

                # Connect in configuration zero
                conn0 = self.connect(job, pcs, 0)

                # Wait for configuration one
                self.sem_config_one_rdy.release()
                self.sem_config_one.acquire()

                # Connect in configuration one
                conn1 = self.connect(job, pcs, 1)

                # Signal okay to go to configuration zero
                self.sem_config_zero_rdy.release()

                # Pass results on for merge
                self.resqueue.put(self.post_connect(job, conn0, pcs, 0))
                self.resqueue.put(self.post_connect(job, conn1, pcs, 1))

                logger.debug("job complete: "+repr(job))
                self.jobqueue.task_done()

    def pre_connect(self, job):
        pass

    def connect(self, job, pcs, config):
        raise NotImplementedError("Cannot instantiate an abstract Qofspider")

    def post_connect(self, job, conn, pcs, config):
        raise NotImplementedError("Cannot instantiate an abstract Qofspider")

    def qofowner(self):
        logger = logging.getLogger('qofspider')

        with tempfile.TemporaryDirectory(prefix="qoftmp") as confdir:
            # create a QoF configuration file
            confpath = os.path.join(confdir, "qof.yaml")
            with open(confpath, "w") as conffile:
                conffile.write(yaml.dump(self.qof_config()))
                logger.debug("wrote qof configuration file in "+confpath)

            # run qof
            qofargs = ["sudo", "-n",
                         "qof", "--yaml", confpath,
                         "--verbose",
                         "--in", self.interface_uri,
                         "--out", "localhost",
                         "--ipfix", "tcp",
                         "--ipfix-port", str(self.qof_port)]
            self.qofproc = subprocess.Popen(qofargs)
            logger.debug("started qof as "+" ".join(qofargs))

            # wait for it to exit
            rv = self.qofproc.wait()
            logger.debug("qof terminated with rv "+str(rv))

            if (rv > 0):
                raise Exception("qof terminated with error "+str(rv))

            if (rv < 0 and rv != -15):
                raise Exception("qof terminated with signal "+str(-rv))

    def qof_config(self):
        raise NotImplementedError("Cannot instantiate an abstract Qofspider")

    class QofCollectorHandler(socketserver.StreamRequestHandler):
        def handle(self):
            logger = logging.getLogger('qofspider')
            logger.info("connection from "+str(self.client_address))

            msr = ipfix.reader.from_stream(self.rfile)

            for d in msr.namedict_iterator():
                tf = self.server.spider.tupleize_flow(d)
                if tf:
                    self.server.spider.flowqueue.put(tf)

            logger.info("connection from "+str(self.client_address)+ "terminated")

    class QofCollectorListener(socketserver.ThreadingMixIn, socketserver.TCPServer):
        def __init__(self, server_address, RequestHandlerClass, spider):
            super().__init__(server_address, RequestHandlerClass)
            self.spider = spider

    def qoflistener(self):
        logger = logging.getLogger('qofspider')
        self.listener = QofSpider.QofCollectorListener(("", self.qof_port),
                QofSpider.QofCollectorHandler, self)
        logger.info("starting listener: "+repr(self.listener))
        self.listener.serve_forever()
        if self.stopping is False:
            logger.error("listener stopped unexpected")
        else:
            logger.debug("listener stopped")

    def tupleize_flow(self, flow):
        raise NotImplementedError("Cannot instantiate an abstract Qofspider")

    def merger(self):
        logger = logging.getLogger('qofspider')
        while self.running:
            if self.flowqueue.qsize() >= self.resqueue.qsize():
                try:
                    flow = self.flowqueue.get_nowait()
                except queue.Empty:
                    time.sleep(QUEUE_SLEEP)

                else:
                    flowkey = (flow.ip, flow.port)
                    logger.debug("got a flow ("+str(flow.ip)+", "+str(flow.port)+")")

                    if flowkey in self.restab:
                        logger.debug("merging flow")
                        self.merge(flow, self.restab[flowkey])
                        del self.restab[flowkey]
                    elif flowkey in self.flowtab:
                        logger.debug("won't merge duplicate flow")
                    else:
                        self.flowtab[flowkey] = flow

                    self.flowqueue.task_done()
            else:
                try:
                    res = self.resqueue.get_nowait()
                except queue.Empty:
                    time.sleep(QUEUE_SLEEP)
                else:
                    reskey = (res.ip, res.port)
                    logger.debug("got a result ("+str(res.ip)+", "+str(res.port)+")")

                    if reskey in self.flowtab:
                        logger.debug("merging result")
                        self.merge(self.flowtab[reskey], res)
                        del self.flowtab[reskey]
                    elif reskey in self.restab:
                        logger.debug("won't merge duplicate result")
                    else:
                        self.restab[reskey] = res

                    self.resqueue.task_done()

    def merge(self, flow, res):
        raise NotImplementedError("Cannot instantiate an abstract Qofspider")

    def exception_wrapper(self, target):
        try:
            target()
        except:
            logger = logging.getLogger('qofspider')
            logger.exception("exception occurred. initiating termination and notify ecnspider component.")
            if self.exception is None:
                self.exception = sys.exc_info()[1]

            self.terminate()

    def run(self):
        logger = logging.getLogger('qofspider')

        logger.info("starting qofspider")

        with self.lock:
            # set the running flag
            self.running = True

            # start the QoF collector
            self.qoflistener_thread = threading.Thread(args=(self.qoflistener,),
                             target=self.exception_wrapper,
                             name='qoflistener',
                             daemon=True)
            self.qoflistener_thread.start()

            # start QoF
            self.qofowner_thread = threading.Thread(args=(self.qofowner,),
                                          target=self.exception_wrapper,
                                          name='qofowner')
            self.qofowner_thread.start()
            logger.debug("waiting "+str(QOF_INITIAL_SLEEP)+"s for qof to start")
            time.sleep(QOF_INITIAL_SLEEP)
            logger.debug("owner up")

            # now start up ecnspider, backwards
            self.merger_thread = threading.Thread(args=(self.merger,),
                             target=self.exception_wrapper,
                             name="merger",
                             daemon=True)
            self.merger_thread.start()
            logger.debug("merger up")

            self.configurator_thread = threading.Thread(args=(self.configurator,),
                             target=self.exception_wrapper,
                             name="configurator",
                             daemon=True)
            self.configurator_thread.start()
            logger.debug("configurator up")

            self.worker_threads = []
            for i in range(self.worker_count):
                t = threading.Thread(args=(self.worker,), target=self.exception_wrapper, name='worker_{}'.format(i), daemon=True)
                self.worker_threads.append(t)
                t.start()

            logger.debug("workers up")

            if self.check_interrupt is not None:
                self.interrupter_thread = threading.Thread(args=(self.interrupter,), target=self.exception_wrapper, name="interrupter", daemon=True)
                self.interrupter_thread.start()
                logger.debug("interrupter up")

    def terminate(self):
        if self.terminating:
            return
        self.terminating = True

        logger = logging.getLogger('qofspider')
        logger.error("terminating qofspider.")

        self.running = False

        # terminate qof, close listeners, join all threads
        try:
            self.terminate_qof()
        except:
            pass
        if self.listener is not None:
            self.listener.shutdown()
            self.listener.server_close()

        self.join_threads()

        # empty all queues, so that stop() does not hang up.
        try:
            while True:
                self.jobqueue.task_done()
        except ValueError:
            pass

        try:
            while True:
                self.resqueue.task_done()
        except ValueError:
            pass

        try:
            while True:
                self.flowqueue.task_done()
        except ValueError:
            pass

        logger.error("termination complete. joined all threads, emptied all queues.")

    def join_threads(self):
        if threading.current_thread() != self.qofowner_thread:
            self.qofowner_thread.join()

        for worker in self.worker_threads:
            if threading.current_thread() != worker:
                worker.join()

        if threading.current_thread() != self.qoflistener_thread:
            self.qoflistener_thread.join()

        if threading.current_thread() != self.configurator_thread:
            self.configurator_thread.join()

        if self.interrupter_thread is not None and threading.current_thread() != self.interrupter_thread:
            self.interrupter_thread.join()

        if threading.current_thread() != self.merger_thread:
            self.merger_thread.join()

    def stop(self):
        logger = logging.getLogger('qofspider')

        logger.info("stopping qofspider")

        with self.lock:
            # Set stopping flag
            self.stopping = True

            # Wait for job and result queues to empty
            self.jobqueue.join()
            self.resqueue.join()
            logger.debug("job and result queues empty")

            # Shut down QoF
            logger.debug("Shutting down QoF...")
            try:
                self.terminate_qof()
            except (ProcessLookupError, subprocess.CalledProcessError):
                pass
            self.qofowner_thread.join()

            time.sleep(QOF_FINAL_SLEEP)

            self.listener.shutdown()
            self.listener.server_close()
            logger.debug("QoF shutdown complete")

            # Wait for flow queue to empty
            self.flowqueue.join()
            logger.debug("flow queue empty")

            # Shut down threads
            self.running = False
            self.stopping = False

            # join threads
            self.join_threads()

    def add_job(self, job):
        if self.stopping or self.terminating:
            return

        self.jobqueue.put(job)

    def terminate_qof(self):
        # We need to hack this, since it runs as root and you can't kill it
        subprocess.check_call(['sudo', '-n', 'kill', str(self.qofproc.pid)])

def local_address(ipv=4, target="path-ams.corvid.ch", port=53):
    if ipv == 4:
        af = socket.AF_INET
    elif ipv == 6:
        af = socket.AF_INET6
    else:
        assert False

    try:
        s = socket.socket(af, socket.SOCK_DGRAM)
        s.connect((target, port))
        return ip_address(s.getsockname()[0])
    except:
        return None
