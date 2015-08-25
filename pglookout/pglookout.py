"""
pglookout - replication monitoring and failover daemon

Copyright (c) 2015 Ohmu Ltd
Copyright (c) 2014 F-Secure

This file is under the Apache License, Version 2.0.
See the file `LICENSE` for details.
"""

from __future__ import print_function
from .cluster_monitor import ClusterMonitor
from .common import (
    create_connection_string, get_connection_info, get_connection_info_from_config_line,
    convert_xlog_location_to_offset, parse_iso_datetime, get_iso_timestamp,
    set_syslog_handler, LOG_FORMAT)
from psycopg2.extensions import adapt
from threading import Thread
import copy
import datetime
import logging
import logging.handlers
import os
import signal
import socket
import subprocess
import sys
import time

try:
    from SocketServer import ThreadingMixIn  # pylint: disable=F0401
    from BaseHTTPServer import HTTPServer  # pylint: disable=F0401
    from SimpleHTTPServer import SimpleHTTPRequestHandler  # pylint: disable=F0401
except ImportError:  # Support Py3k
    from socketserver import ThreadingMixIn  # pylint: disable=F0401
    from http.server import HTTPServer, SimpleHTTPRequestHandler  # pylint: disable=F0401

# Prefer simplejson over json as on Python2.6 json does not play together
# nicely with other libraries as it loads strings in unicode and for example
# SysLogHandler does not like getting syslog facility as unicode string.
try:
    import simplejson as json  # pylint: disable=F0401
except ImportError:
    import json

try:
    from systemd import daemon  # pylint: disable=F0401
except:
    daemon = None


logging.basicConfig(level=logging.DEBUG, format=LOG_FORMAT)


class PgLookout(object):
    def __init__(self, config_path):
        self.log = logging.getLogger("pglookout")
        self.running = True
        self.replication_lag_over_warning_limit = False

        self.config_path = config_path
        self.config = {}
        self.log_level = "DEBUG"

        self.connected_master_nodes = {}
        self.disconnected_master_nodes = {}
        self.connected_observer_nodes = {}
        self.disconnected_observer_nodes = {}
        self.replication_lag_warning_boundary = None
        self.replication_lag_failover_timeout = None
        self.own_db = None
        self.current_master = None
        self.failover_command = None
        self.over_warning_limit_command = None
        self.never_promote_these_nodes = None
        self.primary_conninfo_template = None
        self.cluster_monitor = None
        self.syslog_handler = None
        self.cluster_nodes_change_time = time.time()
        self.load_config()

        signal.signal(signal.SIGHUP, self.load_config)
        signal.signal(signal.SIGINT, self.quit)
        signal.signal(signal.SIGTERM, self.quit)

        self.cluster_state = {}
        self.observer_state = {}
        self.overall_state = {"db_nodes": self.cluster_state, "observer_nodes": self.observer_state,
                              "current_master": self.current_master,
                              "replication_lag_over_warning": self.replication_lag_over_warning_limit}

        self.cluster_monitor = ClusterMonitor(self.config, self.cluster_state,
                                              self.observer_state, self.create_alert_file)
        # cluster_monitor doesn't exist at the time of reading the config initially
        self.cluster_monitor.log.setLevel(self.log_level)
        self.webserver = WebServer(self.config, self.cluster_state)

        if daemon:  # If we can import systemd we always notify it
            daemon.notify("READY=1")
            self.log.info("Sent startup notification to systemd that pglookout is READY")
        self.log.info("PGLookout initialized, own_hostname: %r, own_db: %r, cwd: %r",
                      socket.gethostname(), self.own_db, os.getcwd())

    def quit(self, _signal=None, _frame=None):
        self.log.warning("Quitting, signal: %r, frame: %r", _signal, _frame)
        self.cluster_monitor.running = False
        self.running = False
        self.webserver.close()

    def load_config(self, _signal=None, _frame=None):
        self.log.debug("Loading JSON config from: %r, signal: %r, frame: %r",
                       self.config_path, _signal, _frame)

        previous_remote_conns = self.config.get("remote_conns")
        try:
            with open(self.config_path) as fp:
                self.config = json.load(fp)
        except:
            self.log.exception("Invalid JSON config, exiting")
            sys.exit(1)

        if previous_remote_conns != self.config.get("remote_conns"):
            self.cluster_nodes_change_time = time.time()

        if self.config.get("autofollow"):
            try:
                self.primary_conninfo_template = get_connection_info(self.config["primary_conninfo_template"])
            except (KeyError, ValueError):
                self.log.exception("Invalid or missing primary_conninfo_template; not enabling autofollow")
                self.config["autofollow"] = False

        if self.cluster_monitor:
            self.cluster_monitor.config = copy.deepcopy(self.config)

        if self.config.get("syslog") and not self.syslog_handler:
            self.syslog_handler = set_syslog_handler(self.config.get("syslog_address", "/dev/log"),
                                                     self.config.get("syslog_facility", "local2"),
                                                     self.log)
        self.own_db = self.config.get("own_db")
        # the levelNames hack is needed for Python2.6
        log_level_name = self.config.get("log_level", "DEBUG")
        if sys.version_info[0] >= 3:
            self.log_level = getattr(logging, log_level_name)
        else:
            self.log_level = logging._levelNames[log_level_name]  # pylint: disable=W0212,E1101
        try:
            self.log.setLevel(self.log_level)
            if self.cluster_monitor:
                self.cluster_monitor.log.setLevel(self.log_level)
        except ValueError:
            print("Problem setting log level %r" % self.log_level)
            self.log.exception("Problem with log_level: %r", self.log_level)
        self.never_promote_these_nodes = self.config.get("never_promote_these_nodes", [])
        # we need the failover_command to be converted into subprocess [] format
        self.failover_command = self.config.get("failover_command", "").split()
        self.over_warning_limit_command = self.config.get("over_warning_limit_command")
        self.replication_lag_warning_boundary = self.config.get("warning_replication_time_lag", 30.0)
        self.replication_lag_failover_timeout = self.config.get("max_failover_replication_time_lag", 120.0)
        self.log.debug("Loaded config: %r from: %r", self.config, self.config_path)

    def write_cluster_state_to_json_file(self):
        """Periodically write a JSON state file to disk"""
        start_time = time.time()
        state_file_path = self.config.get("json_state_file_path", "/tmp/pglookout_state.json")
        try:
            self.overall_state = {"db_nodes": self.cluster_state, "observer_nodes": self.observer_state,
                                  "current_master": self.current_master}
            json_to_dump = json.dumps(self.overall_state, indent=4)
            self.log.debug("Writing JSON state file to: %r, file_size: %r", state_file_path, len(json_to_dump))
            with open(state_file_path + ".tmp", "w") as fp:
                fp.write(json_to_dump)
            os.rename(state_file_path + ".tmp", state_file_path)
            self.log.debug("Wrote JSON state file to disk, took %.4fs", time.time() - start_time)
        except:
            self.log.exception("Problem in writing JSON: %r file to disk, took %.4fs",
                               self.overall_state, time.time() - start_time)

    def create_node_map(self, cluster_state, observer_state):
        standby_nodes, master_node, master_host = {}, None, None
        connected_master_nodes, disconnected_master_nodes = {}, {}
        connected_observer_nodes, disconnected_observer_nodes = {}, {}
        self.log.debug("Creating node map out of cluster_state: %r and observer_state: %r",
                       cluster_state, observer_state)
        for host, state in cluster_state.items():
            if 'pg_is_in_recovery' in state:
                if state['pg_is_in_recovery']:
                    standby_nodes[host] = state
                elif state['connection']:
                    connected_master_nodes[host] = state
                elif not state['connection']:
                    disconnected_master_nodes[host] = state
            else:
                self.log.debug("No knowledge on host: %r state: %r of whether it's in recovery or not", host, state)

        for observer_name, state in observer_state.items():
            connected = state.get("connection", False)
            if connected:
                connected_observer_nodes[observer_name] = state.get("fetch_time")
            else:
                disconnected_observer_nodes[observer_name] = state.get("fetch_time")
            for host, db_state in state.items():
                if host not in cluster_state:
                    # A single observer can observe multiple different replication clusters.
                    # Ignore data on nodes that don't belong in our own cluster
                    self.log.debug("Ignoring node: %r since it does not belong into our own replication cluster.", host)
                    continue
                if isinstance(db_state, dict):  # other keys are "connection" and "fetch_time"
                    own_fetch_time = parse_iso_datetime(cluster_state.get(host, {"fetch_time": get_iso_timestamp(datetime.datetime(year=2000, month=1, day=1))})['fetch_time'])  # pylint: disable=C0301
                    observer_fetch_time = parse_iso_datetime(db_state['fetch_time'])
                    self.log.debug("observer_name: %r, dbname: %r, state: %r, observer_fetch_time: %r",
                                   observer_name, host, db_state, observer_fetch_time)
                    if 'pg_is_in_recovery' in db_state:
                        if db_state['pg_is_in_recovery']:
                            # we always trust ourselves the most for localhost, and
                            # in case we are actually connected to the other node
                            if observer_fetch_time >= own_fetch_time and host != self.own_db and standby_nodes.get(host, {"connection": False})['connection'] is False:  # pylint: disable=C0301
                                standby_nodes[host] = db_state
                        else:
                            master_node = connected_master_nodes.get(host, {})
                            connected = master_node.get("connection", False)
                            self.log.debug("Observer: %r sees %r as master, we see: %r, same_master: %r, connection: %r",
                                           observer_name, host, self.current_master, host == self.current_master,
                                           db_state.get('connection'))
                            if observer_fetch_time >= own_fetch_time and host != self.own_db:
                                if connected:
                                    connected_master_nodes[host] = db_state
                                else:
                                    disconnected_master_nodes[host] = db_state
                    else:
                        self.log.warning("No knowledge on if: %r %r from observer: %r is in recovery",
                                         host, db_state, observer_name)

        self.connected_master_nodes = connected_master_nodes
        self.disconnected_master_nodes = disconnected_master_nodes
        self.connected_observer_nodes = connected_observer_nodes
        self.disconnected_observer_nodes = disconnected_observer_nodes

        if len(self.connected_master_nodes) == 0:
            self.log.warning("No known master node, disconnected masters: %r", list(disconnected_master_nodes.keys()))
            if len(disconnected_master_nodes) > 0:
                master_host, master_node = list(disconnected_master_nodes.items())[0]
        elif len(self.connected_master_nodes) == 1:
            master_host, master_node = list(connected_master_nodes.items())[0]
            if disconnected_master_nodes:
                self.log.warning("Picked %r as master since %r are in a disconnected state",
                                 master_host, disconnected_master_nodes)
        else:
            self.create_alert_file("multiple_master_warning")
            self.log.error("More than one master node connected_master_nodes: %r, disconnected_master_nodes: %r",
                           connected_master_nodes, disconnected_master_nodes)

        return master_host, master_node, standby_nodes

    def check_cluster_state(self):
        master_node = None
        cluster_state = copy.deepcopy(self.cluster_state)
        observer_state = copy.deepcopy(self.observer_state)
        if not cluster_state:
            self.log.warning("No cluster state, probably still starting up")
            return

        master_host, master_node, standby_nodes = self.create_node_map(cluster_state, observer_state)  # pylint: disable=W0612

        if master_host and master_host != self.current_master:
            self.log.info("New master node detected: old: %r new: %r: %r", self.current_master, master_host, master_node)
            self.current_master = master_host
            if self.own_db and self.own_db != master_host and self.config.get("autofollow"):
                self.start_following_new_master(master_host)

        own_state = self.cluster_state.get(self.own_db)

        # If we're an observer ourselves, we'll grab the IP address from HTTP server address
        observer_info = ','.join(observer_state.keys()) or 'no'
        if not self.own_db:
            observer_info = self.config.get("http_address", observer_info)

        self.log.debug("Cluster has %s standbys, %s observers and %s as master, own_db: %r, own_state: %r",
                       ','.join(standby_nodes.keys()) or 'no',
                       observer_info,
                       self.current_master,
                       self.own_db,
                       own_state or "observer")

        if self.own_db:
            if self.own_db == self.current_master:
                # We are the master of this cluster, nothing to do
                self.log.debug("We %r: %r are still the master node: %r of this cluster, nothing to do.",
                               self.own_db, own_state, master_node)
                return
            if not standby_nodes:
                self.log.warning("No standby nodes set, master node: %r", master_node)
                return
            self.consider_failover(own_state, master_node, standby_nodes)

    def consider_failover(self, own_state, master_node, standby_nodes):
        if not master_node:
            # no master node at all in the cluster?
            self.log.warning("No master node in cluster, %r standby nodes exist, %.2f seconds since last cluster config update, failover timeout set to %r seconds",
                             len(standby_nodes), time.time() - self.cluster_nodes_change_time, self.replication_lag_failover_timeout)
            if self.current_master:
                # we've seen a master at some point in time, but now it's
                # missing, perform an immediate failover to promote one of
                # the standbys
                self.log.warning("Performing failover decision because existing master node disappeared from configuration")
                self.do_failover_decision(own_state, standby_nodes)
                return
            elif (time.time() - self.cluster_nodes_change_time) >= self.replication_lag_failover_timeout:
                # we've never seen a master and more than failover_timeout
                # seconds have passed since last config load (and start of
                # connection attempts to other nodes); perform failover
                self.log.warning("Performing failover decision because no master node was seen in cluster before timeout")
                self.do_failover_decision(own_state, standby_nodes)
                return
        self.check_replication_lag(own_state, standby_nodes)

    def check_replication_lag(self, own_state, standby_nodes):
        replication_lag = own_state.get('replication_time_lag')
        if not replication_lag:
            self.log.warning("No replication lag set in own node state: %r", own_state)
            return
        if replication_lag >= self.replication_lag_warning_boundary:
            self.log.warning("Replication time lag has grown to: %r which is over WARNING boundary: %r, %r",
                             replication_lag, self.replication_lag_warning_boundary,
                             self.replication_lag_over_warning_limit)
            if not self.replication_lag_over_warning_limit:  # we just went over the boundary
                self.replication_lag_over_warning_limit = True
                self.create_alert_file("replication_delay_warning")
                if self.over_warning_limit_command:
                    self.log.warning("Executing over_warning_limit_command: %r", self.over_warning_limit_command)
                    return_code = self.execute_external_command(self.over_warning_limit_command)
                    self.log.warning("Executed over_warning_limit_command: %r, return_code: %r",
                                     self.over_warning_limit_command, return_code)
                else:
                    self.log.warning("No over_warning_limit_command set")
        elif self.replication_lag_over_warning_limit:
            self.replication_lag_over_warning_limit = False
            self.delete_alert_file("replication_delay_warning")

        if replication_lag >= self.replication_lag_failover_timeout:
            self.log.warning("Replication time lag has grown to: %r which is over CRITICAL boundary: %r"
                             ", checking if we need to failover",
                             replication_lag, self.replication_lag_failover_timeout)
            self.do_failover_decision(own_state, standby_nodes)
        else:
            self.log.debug("Replication lag was: %r, other nodes status was: %r", replication_lag, standby_nodes)

    def get_replication_positions(self, standby_nodes):
        self.log.debug("Getting replication positions from: %r", standby_nodes)
        known_replication_positions = {}
        for hostname, node_state in standby_nodes.items():
            now = datetime.datetime.utcnow()
            if node_state['connection'] and \
                now - parse_iso_datetime(node_state['fetch_time']) < datetime.timedelta(seconds=20) and \
                hostname not in self.never_promote_these_nodes:  # noqa # pylint: disable=C0301
                # use pg_last_xlog_receive_location if it's available,
                # otherwise fall back to pg_last_xlog_replay_location but
                # note that both of them can be None.  We prefer
                # receive_location over replay_location as some nodes may
                # not yet have replayed everything they've received, but
                # also consider the replay location in case receive_location
                # is empty as a node that has been brought up from backups
                # without ever connecting to a master will not have an empty
                # pg_last_xlog_receive_location
                lsn = node_state['pg_last_xlog_receive_location'] or node_state['pg_last_xlog_replay_location']
                xlog_pos = convert_xlog_location_to_offset(lsn) if lsn else 0
                known_replication_positions.setdefault(xlog_pos, set()).add(hostname)
        return known_replication_positions

    def _have_we_been_in_contact_with_the_master_within_the_failover_timeout(self):
        # no need to do anything here if there are no disconnected masters
        if len(self.disconnected_master_nodes) > 0:
            disconnected_master_node = list(self.disconnected_master_nodes.values())[0]
            db_time = disconnected_master_node.get('db_time', get_iso_timestamp()) or get_iso_timestamp()
            time_since_last_contact = datetime.datetime.utcnow() - parse_iso_datetime(db_time)
            if time_since_last_contact < datetime.timedelta(seconds=self.replication_lag_failover_timeout):
                self.log.debug("We've had contact with master: %r at: %r within the last %.2fs, not failing over",
                               disconnected_master_node, db_time, time_since_last_contact.seconds)
                return True
        return False

    def do_failover_decision(self, own_state, standby_nodes):
        if len(self.connected_master_nodes) > 0 or self._have_we_been_in_contact_with_the_master_within_the_failover_timeout():
            self.log.warning("We still have some connected masters: %r, not failing over", self.connected_master_nodes)
            return

        known_replication_positions = self.get_replication_positions(standby_nodes)
        if not known_replication_positions:
            self.log.warning("No known replication positions, canceling failover consideration")
            return

        #  We always pick the 0th one coming out of sort, so both standbys will pick the same node for promotion
        furthest_along_host = min(known_replication_positions[max(known_replication_positions)])
        self.log.warning("Node that is furthest along is: %r, all replication positions were: %r",
                         furthest_along_host, known_replication_positions)
        total_observers = len(self.connected_observer_nodes) + len(self.disconnected_observer_nodes)
        # +1 in the calculation comes from the master node
        total_amount_of_nodes = len(standby_nodes) + 1 - len(self.never_promote_these_nodes) + total_observers
        size_of_needed_majority = total_amount_of_nodes * 0.5
        amount_of_known_replication_positions = 0
        for known_replication_position in known_replication_positions.values():
            amount_of_known_replication_positions += len(known_replication_position)
        size_of_known_state = amount_of_known_replication_positions + len(self.connected_observer_nodes)
        self.log.debug("Size of known state: %.2f, needed majority: %r, %r/%r", size_of_known_state,
                       size_of_needed_majority, amount_of_known_replication_positions, int(total_amount_of_nodes))

        if standby_nodes[furthest_along_host] == own_state:
            if self.check_for_maintenance_mode_file():
                self.log.warning("Canceling failover even though we were the node the furthest along, since "
                                 "this node has an existing maintenance_mode_file: %r",
                                 self.config.get("maintenance_mode_file", "/tmp/pglookout_maintenance_mode_file"))
                return
            elif self.own_db in self.never_promote_these_nodes:
                self.log.warning("Not doing a failover even though we were the node the furthest along, since this node: %r"
                                 " should never be promoted to master", self.own_db)
            elif size_of_known_state < size_of_needed_majority:
                self.log.warning("Not doing a failover even though we were the node the furthest along, since we aren't "
                                 "aware of the states of enough of the other nodes")
            else:
                start_time = time.time()
                self.log.warning("We will now do a failover to ourselves since we were the host furthest along")
                return_code = self.execute_external_command(self.failover_command)
                self.log.warning("Executed failover command: %r, return_code: %r, took: %.2fs",
                                 self.failover_command, return_code, time.time() - start_time)
                self.create_alert_file("failover_has_happened")
                # Sleep for failover time to give the DB time to restart in promotion mode
                # You want to use this if the failover command is not one that blocks until
                # the db has restarted
                time.sleep(self.config.get("failover_sleep_time", 0.0))
                if return_code == 0:
                    self.replication_lag_over_warning_limit = False
                    self.delete_alert_file("replication_delay_warning")
        else:
            self.log.warning("Nothing to do since node: %r is the furthest along", furthest_along_host)

    def modify_recovery_conf_to_point_at_new_master_host(self, new_master_host):
        path_to_recovery_conf = os.path.join(self.config.get("pg_data_directory"), "recovery.conf")
        with open(path_to_recovery_conf, "r") as fp:
            old_conf = fp.read().splitlines()
        has_recovery_target_timeline = False
        new_conf = []
        old_conn_info = None
        for line in old_conf:
            if line.startswith("recovery_target_timeline"):
                has_recovery_target_timeline = True
            if line.startswith("primary_conninfo"):
                # grab previous entry: strip surrounding quotes and replace two quotes with one
                try:
                    old_conn_info = get_connection_info_from_config_line(line)
                except ValueError:
                    self.log.exception("failed to parse previous %r, ignoring", line)
                continue  # skip this line
            new_conf.append(line)

        # If has_recovery_target_timeline is set and old_conn_info matches
        # new info we don't have to do anything
        new_conn_info = dict(self.primary_conninfo_template, host=new_master_host)
        if new_conn_info == old_conn_info and has_recovery_target_timeline:
            self.log.debug("recovery.conf already contains conninfo matching %r, not updating", new_master_host)
            return False
        # Otherwise we append the new primary_conninfo
        new_conf.append("primary_conninfo = {0}".format(adapt(create_connection_string(new_conn_info))))
        # The timeline of the recovery.conf will require a higher timeline target
        if not has_recovery_target_timeline:
            new_conf.append("recovery_target_timeline = 'latest'")
        # prepend our tag
        new_conf.insert(0, "# pglookout updated primary_conninfo for host {0} at {1}".format(new_master_host, get_iso_timestamp()))
        # Replace old recovery.conf with a fresh copy
        with open(path_to_recovery_conf + "_temp", "w") as fp:
            fp.write("\n".join(new_conf) + "\n")
        self.log.debug("Previous recovery.conf: %s", old_conf)
        self.log.debug("Newly written recovery.conf: %s", new_conf)
        os.rename(path_to_recovery_conf + "_temp", path_to_recovery_conf)
        return True

    def start_following_new_master(self, new_master_host):
        start_time = time.time()
        updated_config = self.modify_recovery_conf_to_point_at_new_master_host(new_master_host)
        if not updated_config:
            self.log.info("Already following master %r, no need to start following it again", new_master_host)
            return
        start_command, stop_command = self.config.get("pg_start_command", "").split(), self.config.get("pg_stop_command", "").split()
        self.log.info("Starting to follow new master %r, modified recovery.conf and restarting PostgreSQL"
                      "; pg_stop_command %r; pg_start_command %r",
                      new_master_host, start_command, stop_command)
        self.execute_external_command(stop_command)
        self.execute_external_command(start_command)
        self.log.info("Started following new master %r, took: %.2fs", new_master_host, time.time() - start_time)

    def execute_external_command(self, command):
        self.log.warning("Executing external command: %r", command)
        return_code, output = 0, ""
        try:
            output = subprocess.check_call(command)
        except subprocess.CalledProcessError as err:
            self.log.exception("Problem with executing: %r, return_code: %r, output: %r",
                               command, err.returncode, err.output)
            return_code = err.returncode  # pylint: disable=E1101
        self.log.warning("Executed external command: %r, output: %r", return_code, output)
        return return_code

    def check_for_maintenance_mode_file(self):
        return os.path.exists(self.config.get("maintenance_mode_file", "/tmp/pglookout_maintenance_mode_file"))

    def create_alert_file(self, filename):
        try:
            filepath = os.path.join(self.config.get("alert_file_dir", os.getcwd()), filename)
            self.log.debug("Creating alert file: %r", filepath)
            with open(filepath, "w") as fp:
                fp.write("alert")
        except:
            self.log.exception("Problem writing alert file: %r", filepath)

    def delete_alert_file(self, filename):
        try:
            filepath = os.path.join(self.config.get("alert_file_dir", os.getcwd()), filename)
            if os.path.exists(filepath):
                self.log.debug("Deleting alert file: %r", filepath)
                os.unlink(filepath)
        except:
            self.log.exception("Problem unlinking: %r", filepath)

    def main_loop(self):
        while self.running:
            # Separate try/except so we still write the state file
            sleep_time = 5.0
            try:
                sleep_time = float(self.config.get("replication_state_check_interval", 5.0))
                self.check_cluster_state()
            except:
                self.log.exception("Failed to check cluster state")
            try:
                self.write_cluster_state_to_json_file()
            except:
                self.log.exception("Failed to write cluster state")
            time.sleep(sleep_time)

    def run(self):
        self.cluster_monitor.start()
        self.webserver.start()
        self.main_loop()


class ThreadedWebServer(ThreadingMixIn, HTTPServer):
    cluster_state = None
    log = None


class WebServer(Thread):
    def __init__(self, config, cluster_state):
        Thread.__init__(self)
        self.config = config
        self.cluster_state = cluster_state
        self.log = logging.getLogger("WebServer")
        self.address = self.config.get("http_address", '')
        self.port = self.config.get("http_port", 15000)
        self.server = None
        self.log.debug("WebServer initialized with address: %r port: %r", self.address, self.port)

    def run(self):
        # We bind the port only when we start running
        self.server = ThreadedWebServer((self.address, self.port), RequestHandler)
        self.server.cluster_state = self.cluster_state
        self.server.log = self.log
        self.server.serve_forever()

    def close(self):
        self.log.debug("Closing WebServer")
        self.server.shutdown()
        self.log.debug("Closed WebServer")


class RequestHandler(SimpleHTTPRequestHandler):
    def do_GET(self):
        self.server.log.debug("Got request: %r", self.path)
        if self.path.startswith("/state.json"):
            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            response = json.dumps(self.server.cluster_state, indent=4)
            self.send_header('Content-length', len(response))
            self.end_headers()
            self.wfile.write(response)
        else:
            self.send_response(404)


def main(args=None):
    if not args:
        args = sys.argv[1:]
    if len(args) == 1 and os.path.exists(args[0]):
        pglookout = PgLookout(args[0])
        pglookout.run()
    else:
        print("Usage, pglookout <config filename>")
        return 1

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
