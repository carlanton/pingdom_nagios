#!/usr/bin/env python
# -*- encoding: utf-8 -*-
from __future__ import print_function
import argparse
import base64
import errno
import fcntl
import httplib
import json
import logging
import logging.handlers
import os
import re
import sys
import urllib
from socket import timeout

"""
 _____ _           _
|  _  |_|___ ___ _| |___ _____
|   __| |   | . | . | . |     |
|__|  |_|_|_|_  |___|___|_|_|_|
            |___|
External services queue manager

"""
#  Default settings

settings = {
    'log_to_syslog': True,
    'api_base': "api.pingdom.com/api/3.0",
    'queue_file': "/tmp/pingdom_nagios_queue",
    'api_timeout': 15
}

logger = logging.getLogger("pingdom-nagios")

if settings['log_to_syslog']:
    handler = logging.handlers.SysLogHandler(address='/dev/log')
    formatter = logging.Formatter("%(name)s[%(process)s]: %(message)s")
    handler.setFormatter(formatter)
    logger.addHandler(handler)


def post_event(event):
    params = {
        'source': 'service',
        'data_type': 'nagios',
        'triggerid': event['CONTACTPAGER'],
        'data': json.dumps(event)
    }

    post_params = urllib.urlencode(params)
    auth = base64.encodestring('anonymous:anonymous').rstrip()
    domain, _, page = settings['api_base'].partition('/')

    headers = {"Content-type": "application/x-www-form-urlencoded",
               "Accept": "*/*",
               "Authorization": "Basic %s" % auth}

    if page:
        url = "/%s/%s" % (page, 'ims.incidents')
    else:
        url = "/%s" % 'ims.incidents'

    conn = httplib.HTTPSConnection(domain, timeout=settings['api_timeout'])
    conn.request("POST", url, post_params, headers)
    resp = conn.getresponse()

    return resp


def read_events():
    try:
        with open(settings['queue_file'], 'r') as fd:
            try:
                return list(json.load(fd))
            except ValueError:
                logger.warning("Invalid JSON in queue file %s, discarding." %
                               settings['queue_file'])
                return []

    except IOError, e:
        if e.errno == errno.ENOENT:
            return []
        logger.error("Error while reading event queue: %s." % e)


def write_events(events):
    try:
        with open(settings['queue_file'], 'w') as fd:
            return json.dump(events, fd)
    except IOError, e:
        logger.error("Error while writing event queue: %s." % e)


def send_queued_events():
    events = read_events()

    while events:
        event = events[0]

        try:
            resp = post_event(event)
        except timeout:
            logger.warning("BeepManager server timed out, deferring event.")
            write_events(events)
            return False

        if resp.status == 200:
            events.pop(0)
        elif resp.status >= 400 and resp.status < 500:
            logger.warning("Event rejected by BeepManager. HTTP %d: %s"
                           % (resp.status, resp.read()))
            events.pop(0)
        else:
            logger.warning("BeepManager server error HTTP %d, "
                           "deferring event." % resp.status)
            write_events(events)
            return False

    write_events(events)
    return True


def exclusive_action(action):
    lock_filename = settings['queue_file'] + ".lock"
    try:
        with open(lock_filename, 'w') as lock_fd:
            try:
                fcntl.flock(lock_fd.fileno(), fcntl.LOCK_EX)
            except IOError, e:
                logger.error("flock %s failed: %s", (lock_filename, e))
                return False

            ret = action()

    except IOError, e:
        logger.error("open %s for write failed: %s" % (lock_filename, e))
        return False

    os.remove(lock_filename)

    return ret


def queue_events():
    pattern = re.compile("^(ICINGA|NAGIOS)_(.*)$")
    event = {}

    for k, v in os.environ.items():
        match = pattern.match(k)
        if match:
            event[match.group(2)] = v

    if event:
        if 'CONTACTPAGER' not in event:
            logger.error("No pager set in event, ignoring.")
            return

        events = read_events()
        events.append(event)
        write_events(events)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--queue', dest='queue', action='store_true',
                        help="Queue events")
    parser.add_argument('--send', dest='send', action='store_true',
                        help="Send queued events")

    args = parser.parse_args(sys.argv[1:])

    if args.queue:
        exclusive_action(queue_events)

    if args.send:
        exclusive_action(send_queued_events)
