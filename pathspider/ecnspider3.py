
import sys
import logging
import subprocess

import socket
import collections

from pathspider.base import Spider
from pathspider.observer import Observer
from pathspider.observer import basic_flow
from pathspider.observer import basic_count

Connection = collections.namedtuple("Connection", ["client", "port", "state"])
SpiderRecord = collections.namedtuple("SpiderRecord", ["ip", "rport", "port",
                                                       "host", "ecnstate",
                                                       "connstate"])

CONN_OK = 0
CONN_FAILED = 1
CONN_TIMEOUT = 2

USER_AGENT = "pathspider"

## Chain functions

def tcpcompleted(rec, tcp, rev): # pylint: disable=W0612,W0613
    return not tcp.fin_flag

## ECNSpider main class

class ECNSpider(Spider):


    def __init__(self, worker_count, libtrace_uri, check_interrupt=None):
        super().__init__(worker_count=worker_count,
                         libtrace_uri=libtrace_uri,
                         check_interrupt=check_interrupt)
        self.tos = None # set by configurator
        self.conn_timeout = 10

    def config_zero(self):
        logger = logging.getLogger('ecnspider3')
        subprocess.check_call(['/sbin/sysctl', '-w', 'net.ipv4.tcp_ecn=2'],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        logger.info("Configurator disabled ECN")

    def config_one(self):
        logger = logging.getLogger('ecnspider3')
        subprocess.check_call(['/sbin/sysctl', '-w', 'net.ipv4.tcp_ecn=1'],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        logger.info("Configurator enabled ECN")

    def connect(self, job, pcs, config):
        sock = socket.socket()

        try:
            sock.settimeout(self.conn_timeout)
            sock.connect((job[0], job[1]))

            return Connection(sock, sock.getsockname()[1], CONN_OK)
        except TimeoutError:
            return Connection(sock, sock.getsockname()[1], CONN_TIMEOUT)
        except OSError:
            return Connection(sock, sock.getsockname()[1], CONN_FAILED)

    def post_connect(self, job, conn, pcs, config):
        if conn.state == CONN_OK:
            rec = SpiderRecord(job[0], job[1], conn.port, job[2], config, True)
        else:
            rec = SpiderRecord(job[0], job[1], conn.port, job[2], config, False)

        try:
            conn.client.shutdown(socket.SHUT_RDWR)
        except:
            pass

        try:
            conn.client.close()
        except:
            pass

        return rec

    def create_observer(self):
        logger = logging.getLogger('ecnspider3')
        logger.info("Creating observer")
        try:
            return Observer(self.libtrace_uri,
                            new_flow_chain=[basic_flow],
                            ip4_chain=[basic_count],
                            ip6_chain=[basic_count],
                            tcp_chain=[tcpcompleted])
        except:
            logger.error("Observer not cooperating, abandon ship")
            sys.exit()

    def merge(self, flow, res):
        logger = logging.getLogger('ecnspider3')
        flow['connstate'] = res.connstate
        flow['ecnstate'] = res.ecnstate
        logger.info("Result: " + str(flow))
        self.merged_results.append(flow)

