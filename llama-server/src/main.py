'''
Main application entrypoint, Flask server.
'''


import json
import logging
import threading

from flask import Flask, request, jsonify, render_template
from flask_expects_json import expects_json
from datetime import datetime
from pympler import asizeof
from time import sleep

from common.functions import create_date
from models.flask_schema import registration_schema
from helpers.influxdb import write_influx, setup_influx, metrics_log_point
from helpers.probe import is_probe_dup
from helpers.config import load_conf
from helpers.loadtest import loadtest_register_probe

app = Flask(__name__)

# Global variable to lock threads as needed
thread_lock = threading.Lock()

# TODO Make the homepage show manual
@app.route('/', methods=['GET'])
def home():
    return "<h1>Welcome Home!</h1><p>Generic HomePage</p>"


# TODO: Prometheus or JSON metrics and stats go here
@app.route('/metrics', methods=['GET'])
def get_metrics():
    return jsonify(metrics), 200


# Returns IP address of requester, for finding NAT/Public address in the future
@app.route("/api/v1/my_ip_address", methods=['GET'])
def my_ip_address():
    if request.headers.getlist("X-Forwarded-For"):
        return jsonify({'ip': request.headers.getlist("X-Forwarded-For")[0]}), 200
    else:
        return jsonify({'ip': request.remote_addr}), 200


# Interval metric
@app.route("/api/v1/interval", methods=['GET'])
def interval():
    return str(config.interval), 200


# Registration endpoint
@app.route("/api/v1/register", methods=['POST'])
@expects_json(registration_schema, fill_defaults=True)
def add_entry():
    request_json = request.get_json()

    # Add create date to the json data
    request_json.update(create_date())

    # If IP address was not set, try to figure out what it should be. "If key NOT IN dict"
    if not request_json.get('ip'):
        # Add requestor IP address to the json data, If X-Forwarded is present use that
        if request.headers.getlist("X-Forwarded-For"):
            request_json.update({'ip': '%s' % request.headers.getlist("X-Forwarded-For")[0]})
        else:
            request_json.update({'ip': '%s' % request.remote_addr})

    # Formulate probe ID by "IP:Port", ex: "192.168.1.12:8100"
    request_json.update({'id': '%s:%s' % (request_json['ip'], request_json['port'])})

    logging.debug("Registration Update: '%s'" % request_json['id'])

    # Wait for thread lock in the event cleanup is running
    thread_lock.acquire()

    # If the group does not exsist, create it as a source key in the database
    if request_json['group'] not in database:
        database[request_json['group']] = {}

    # Turn "id" into a key to organize hosts, incert to database variable
    database[request_json['group']][request_json['id']] = request_json
    # Release thread lock
    thread_lock.release()

    return database


# A route to return all of the available entries
@app.route('/api/v1/list', methods=['GET'])
def api_list_all():
    return jsonify(database), 200


# A route to return a list of hosts that the scraper will collect from
@app.route('/api/v1/scraper', methods=['GET'])
def api_scraper():
    # Dont reply with data if loadtest is running, bad things will happen
    if config.loadtest:
        return "<h1>Loadtest Is Running</h1><p>A loadtest is running, this call is disabled.</p>"

    hosts = []
    # Cycle through all groups to formulate a list
    for group in database:
        for host in database[group]:
            hosts.append(database[group][host]['ip'])
    logging.debug("Scraper Host List: %s" % hosts)


    # If list is empty, no hosts have joined, return 127.0.0.1
    if len(hosts) == 0:
        hosts.append("127.0.0.1")

    # Turn the host LIST into a comma separated string
    joined_string = ",".join(hosts)

    return render_template("scraper.j2", hosts=joined_string)


# A route to return LLAMA Collector config file via template
@app.route('/api/v1/config/<group>', methods=['GET'])
def api_config(group):
    # Create a temporary database for data manipulation, we dont want this perm
    #database_tmp = database.copy()
    # Apparently thread safe way to perform a full database copy (not shadow)
    database_tmp = json.loads(json.dumps(database))

    if group in database_tmp:
        # If requesting probe is in the group list, change target IP to 127.0.0.1
        port = request.args.get('llamaport', None)
        reported_source_ip = request.args.get('srcip', None)
        remote_ip_address = request.remote_addr

        # If a Source IP was provided, do things (hack around certain NAT scenario)
        if reported_source_ip:
            logging.info("CONFIG: '%s' says its IP is '%s'" % (request.remote_addr, reported_source_ip))
            # TODO: Verify reported IP address is a valid ipv4 address
            remote_ip_address = reported_source_ip

        # Log if a client is not sending what port it has assigned
        if not port:
            logging.error("No port was given from probe '%s' when generating configuration" % remote_ip_address) 
            port = "null"

        # Store probe ID "IP_ADDRESS:PORT"
        requesting_probe_id = "%s:%s" % (remote_ip_address, port)
        logging.debug("Config request from '%s'" % requesting_probe_id)

        # Check if key not in dict python
        if requesting_probe_id not in database_tmp[group]:
            logging.error("Requesting probe '%s' has not registered, no config will be given" % requesting_probe_id)
            return jsonify({'error': "unknown probe '%s', please register" % requesting_probe_id}), 404

        # Setup source + destination pairs per probe
        for remote_id in database_tmp[group]:

            # Rewrite self to 127.0.0.1
            if remote_id == requesting_probe_id:
                database_tmp[group][requesting_probe_id]["ip"] = "127.0.0.1"
                logging.debug("Local probe translation to 127.0.0.1 - %s" % requesting_probe_id)

                database_tmp[group][requesting_probe_id]["tags"]["dst_name"] = database_tmp[group][requesting_probe_id]["tags"]["probe_name"]
                database_tmp[group][requesting_probe_id]["tags"]["dst_shortname"] = database_tmp[group][requesting_probe_id]["tags"]["probe_shortname"]
                database_tmp[group][requesting_probe_id]["tags"]["src_name"] = database_tmp[group][requesting_probe_id]["tags"]["probe_name"]
                database_tmp[group][requesting_probe_id]["tags"]["src_shortname"] = database_tmp[group][requesting_probe_id]["tags"]["probe_shortname"]
                database_tmp[group][requesting_probe_id]["tags"]["group"] = group
                pass

            database_tmp[group][remote_id]["tags"]["dst_name"] = database_tmp[group][remote_id]["tags"]["probe_name"]
            database_tmp[group][remote_id]["tags"]["dst_shortname"] = database_tmp[group][remote_id]["tags"]["probe_shortname"]
            database_tmp[group][remote_id]["tags"]["src_name"] = database_tmp[group][requesting_probe_id]["tags"]["probe_name"]
            database_tmp[group][remote_id]["tags"]["src_shortname"] = database_tmp[group][requesting_probe_id]["tags"]["probe_shortname"]
            database_tmp[group][remote_id]["tags"]["group"] = group

            # Remove the "probe" name entries, we dont need to send those
            #database_tmp[group][remote_id]["tags"].pop("probe_name", None)
            #database_tmp[group][remote_id]["tags"].pop("probe_shortname", None)

        logging.debug(database_tmp[group])
        return render_template("config.yaml.j2", template_data=database_tmp[group], template_interval=config.interval)

    # If group is not located, error
    logging.error("'/api/v1/config/%s' - Unknown group" % group)
    return jsonify({'error': "unknown group '%s'" % group}), 404


# A route to return a certain group of the available entries
@app.route('/api/v1/list/<group>', methods=['GET'])
def api_list_group(group):
    if group in database:
        return jsonify(database[group]), 200

    # If group is not located, error
    logging.error("'/api/v1/list/%s' - Unknown group" % group)
    return jsonify({'error': "unknown group '%s'" % group}), 404


# Background process that removes stale entries
def clean_stale_probes():
    # Run every 60 seconds
    while(not sleep(60)):
        # Get start time for runtime metrics
        start_time = datetime.now().timestamp()

        # Aquire thread lock for variable work, stops 'RuntimeError: dictionary changed size during iteration'
        with thread_lock:
            logging.warning("Thread Locked!")

            # Initialize list
            remove_probe_list = []
            remove_group_list = []

            # Initialize metric
            remove_probe_count = 0

            # Go through all groups for stale probes
            for group in database:
                # Scann all probes in the inventory, remove those that have aged to long
                for probe in database[group]:
                    # Caclulate current time and creation date to seconds passed
                    age = int((datetime.now() - datetime.strptime(database[group][probe]['create_date'], '%Y-%m-%dT%H:%M:%S.%f')).total_seconds())

                    logging.debug("Probe '%s' in group '%s' checked in %i seconds ago" % (probe, group, age))
                    if age > database[group][probe]['keepalive']:
                        logging.debug("Probe '%s' in group '%s' should be removed!" % (probe, group))
                        remove_probe_list.append(probe)

                    # If there is a duplicate entry mark for deletetion
                    if is_probe_dup(group, probe, database):
                        remove_probe_list.append(probe)

                # Remove old probed from global database
                for item in remove_probe_list:
                    database[group].pop(item, None)

                # Add to metric counter
                remove_probe_count = len(remove_probe_list) + remove_probe_count

                # Clear list
                remove_probe_list = []

            # Warning log on removed probes
            if remove_probe_count > 0:
                logging.warning("Removed %i probe(s) due to aging" % remove_probe_count)

            # If a group is empty add to removal list
            for group in database:
                if not database[group]:
                    logging.warning("Group '%s' is empty, removing it." % group)
                    remove_group_list.append(group)

            # Remove empty groups from global database
            for item in remove_group_list:
                database.pop(item, None)

            # Lets collect and crunch some metrics here
            global metrics

            # Calculate the number of active nodes
            node_count = 0
            for group in database:
                node_count = node_count + len(database[group])
            logging.info("%i active probe(s) are registered" % node_count)

            # Write metrics
            metrics["probe_count_removed"] = remove_probe_count
            metrics["probe_count_active"] = node_count
            metrics["group_count_active"] = len(database)
            metrics["group_count_removed"] = len(remove_group_list)
            metrics["database_size_bytes"] = asizeof.asizeof(database)
            metrics["clean_runtime"] = float(datetime.now().timestamp() - start_time)
            metrics["uptime"] = datetime.now().timestamp() - metrics["start_time"].timestamp()
            metrics["metrics_timestamp"] = datetime.now()
            metrics['active_threads'] = threading.active_count()

        logging.warning("Thread Unlocked!")
        logging.debug(database)

        # Export metrics to InfluxDB
        if config.influxdb_host:
            write_influx(influxdb_client, metrics_log_point(metrics))


def loadtest(config, keepalive: int, sleeptimer=0, max_registration=32000) -> None:
    ''' loadtest thread loop '''

    logging.info("Loadtest thread started...")
    loop_count = 0

    # Sleep for 10 seconds so Flask has time to startup
    sleep(10)

    # Loadtest Loop
    while(not sleep(sleeptimer)):
        loop_count += 1
        loadtest_register_probe(config, keepalive)
        # If we set a limited number of registrations
        if loop_count > max_registration:
            logging.warning("Max registrations completed, count: '%i'" % loop_count)
            break
        #logging.debug("LoadTest probe registration count: %i" % loop_count)


if __name__ == "__main__":
    # Initialize dictionaries
    database = {}
    metrics = {}

    # Generate configruation
    config = load_conf()

    # Gather application start time for metrics and data validation
    metrics["start_time"] = datetime.now()

    if config.influxdb_host:
        for i in range(3):
            # Creat influxDB if none exsists and option enabled
            logging.info("Setting up InfluxDB database '%s' on '%s:%s' attempt %i of 3" % (config.influxdb_name, config.influxdb_host, config.influxdb_port, i+1))
            influxdb_client = setup_influx(config)

            # If client is not None, then database connection has ben created
            if influxdb_client:
                logging.info("InfluxDB connection verified!")
                break

            # Escalating sleep timer, 5sec -> 10sec -> 15sec
            sleep((i+1)*5)

    # Start bcakground threaded process to clean stale probes
    inline_thread_cleanup = threading.Thread(target=clean_stale_probes, name="CleanThread")
    inline_thread_cleanup.start()

    # Start loadtesting if option is selected.
    if config.loadtest:
        # Aggresivly adds 5k probes
        inline_loadtest01 = threading.Thread(target=loadtest, args=(config, 6000, 0.01, 5000), name="LoadTestThread01")
        inline_loadtest01.start()
        # Slower and long-term probe adds
        inline_loadtest03= threading.Thread(target=loadtest, args=(config, 84000, 0.05, 84000), name="LoadTestThread03")
        inline_loadtest03.start()
        # Adds a probe we expect to timeout via keepalive, ~50-60min
        inline_loadtest_cleanup= threading.Thread(target=loadtest, args=(config, 1, 1), name="LoadTestThreadCleanup")
        inline_loadtest_cleanup.start()

    logging.info("Flask server started on '%s:%s'" % (config.host, config.port))

    # Te get flask out of development mode
    # https://stackoverflow.com/questions/51025893/flask-at-first-run-do-not-use-the-development-server-in-a-production-environmen
    from waitress import serve
    serve(app, host=config.host, port=config.port, threads=8)
