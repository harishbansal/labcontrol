#!/usr/bin/python
# vim: set ts=4 sw=4 et :
#
# lcserver.py - LabControl server CGI script
#
# Copyright 2020 Sony
#
# Implementation notes:
#  data directory = place where json object files are stored
#  files directory = place where file bundles are stored
#  pages directory = place where web page templates are stored
#
#
# The server implements three interfaces:
#  1. the human user interace (web pages showing objects and available
#  actions)
#  2. a human user interace showing raw object (files and json contents)
#  3. the computer ReST interface (used for sending, modifying and
#     retrieving the data in the store, and for performing REST API actions
#     on the objects)
#
# The server currently supports the top-level "pages":
#   boards, resources, requests, logs
#
# The REST API specified with Timesys uses urls like this:
#  /api/v0.2/devices/bbb/power/reboot
# I'm calling this the 'path api'
#
# To do:
# - convert everything over to the path api
#   + list devices
# - actions:
#    + support assign, release and release/force (lc allocate/release)
#    + support api/v0.2/devices/mine (lc mydevices)
# - queries:
#   - handle regex wildcards instead of just start/end wildcards
# - objects:
#   - support board registration - put-board
#   - support resource registration - put-resource
#   - support host registration
#   - support user registration
# - requests:
# - security:
#   - add otp authentication to all requests
#     - check host's otp file for specified key
#     - erase key after use
# - add hosts (or users)
#    - so we can: 1) save an otp file, 2) validate requests?
# - see also items marked with FIXTHIS
#

import sys
import os
import time
import cgi
import re
import tempfile
import urllib

# simplejson loads faster than json, use that if available
try:
    import simplejson as json
except ImportError:
    import json

# import yaml as needed
#import yaml
import copy
import shlex
import subprocess
import signal
import threading   # used for Timer objects

debug = False
#debug = True

debug_api_response = False
# uncomment this to dump response data to the log file
#debug_api_response = True

# Keep track of which getstatusoutput I'm using
using_commands_gso = False
try:
    from subprocess import getstatusoutput
except:
    from commands import getstatusoutput
    using_commands_gso = True

VERSION=(0,6,0)

# precedence of installation locations:
# 2. local lcserver in Fuego container
# 3. test lcserver on Tim's private server machine (birdcloud.org)
# 4. test lcserver on Tim's home desktop machine (timdesk)
base_dir = "/home/ubuntu/work/labcontrol/lc-data"
if not os.path.exists(base_dir):
    base_dir = "/usr/local/lib/labcontrol/lc-data"
if not os.path.exists(base_dir):
    base_dir = "/home/tbird/work/labcontrol/lc-data"

RSLT_FAIL="fail"
RSLT_OK="success"

# this is used for debugging only
def log_this(msg):
    with open(base_dir+"/lcserver.log" ,"a") as f:
        f.write("[%s] %s\n" % (get_timestamp(), msg))

def dlog_this(msg):
    global debug

    if debug:
        with open(base_dir+"/lcserver.log" ,"a") as f:
            f.write("[%s] %s\n" % (get_timestamp(), msg))

# define an instance to hold config vars
class config_class:
    def __init__(self):
        pass

    def __getitem__(self, name):
        return self.__dict__[name]

config = config_class()
config.data_dir = base_dir + "/data"

# crude attempt at auto-detecting url_base
if os.path.exists("/usr/lib/cgi-bin/lcserver.py"):
    config.url_base = "/cgi-bin/lcserver.py"
else:
    config.url_base = "/lcserver.py"

config.files_url_base = "/lc-data"
config.files_dir = base_dir + "/files"
config.page_dir = base_dir + "/pages"

class req_class:
    def __init__(self, config, form):
        self.config = config
        self.header_shown = False
        self.message = ""
        self.page_name = ""
        self.page_url = "page_name_not_set_error"
        self.form = form
        self.html = []
        self.api_path = ""
        self.obj_path = ""
        self.user = None

    def set_page_name(self, page_name):
        page_name = re.sub(" ","_",page_name)
        self.page_name = page_name
        self.page_url = self.make_url(page_name)

    def page_filename(self):
        if not hasattr(self, "page_name"):
            raise AttributeError, "Missing attribute"
        return self.config.page_dir+os.sep+self.page_name

    def read_page(self, page_name=""):
        if not page_name:
            page_filename = self.page_filename()
        else:
                page_filename = self.config.page_dir+os.sep+page_name

        return open(page_filename).read()

    def make_url(self, page_name):
        page_name = re.sub(" ","_",page_name)
        return self.config.url_base+"/"+page_name

    def html_escape(self, str):
        str = re.sub("&","&amp;",str)
        str = re.sub("<","&lt;",str)
        str = re.sub(">","&gt;",str)
        return str

    def add_to_message(self, msg):
        self.message += msg + "<br>\n"

    def add_msg_and_traceback(self, msg):
        self.add_to_message(msg)
        import traceback
        tb = traceback.format_exc()
        self.add_to_message("<pre>\n%s\n</pre>\n" % tb)

    def show_message(self):
        if self.message:
            self.html.append("<h2>lcserver message(s):</h2>")
            self.html.append(self.message)

    def show_header(self, title):
        if self.header_shown:
            return

        self.header = """Content-type: text/html\n\n"""

        # render the header markup
        self.html.append(self.header)
        self.html.append('<body><h1 align="center">%s</h1>' % title)
        self.header_shown = True

    def show_footer(self):
        self.show_message()
        self.html.append("</body>")

    def html_error(self, msg):
        return "<font color=red>" + msg + "</font><BR>"

    def send_response(self, result, data):
        self.html.append("Content-type: text/plain\n\n%s\n" % result)
        self.html.append(data)

    # API responses: return python dictionary as json data
    def send_api_response(self, result, data = {}):
        data["result"] = result

        json_data = json.dumps(data, sort_keys=True, indent=4,
            separators=(',', ': '))

        if debug_api_response:
            log_this("response json_data=%s" % json_data)

        self.html.append("Content-type: text/plain\n\n")
        self.html.append(json_data)

    def send_api_response_msg(self, result, msg):
        self.send_api_response(result, { "message": msg })

    def send_api_list_response(self, data):

        json_data = json.dumps(data, sort_keys=True, indent=4,
            separators=(',', ': '))

        self.html.append("Content-type: text/plain\n\n")
        self.html.append(json_data)

    def get_user(self):
        # returns valid user name or None
        user = None

        AUTH_TYPE = self.environ.get("AUTH_TYPE", "none")
        if AUTH_TYPE != "token":
            return user
        http_auth = self.environ.get("HTTP_AUTHORIZATION", "nobody")
        if http_auth == "nobody":
            return user

        # scan user files for matching authentication token
        token = http_auth.split()[1]
        if token == "not-a-valid-token":
            return user

        user_dir = self.config.data_dir + "/users"
        try:
            user_files = os.listdir( user_dir )
        except:
            log_this("Error: could not read user files from " + user_dir)
            return user

        for ufile in user_files:
            upath = user_dir + "/" + ufile
            try:
                ufd = open(upath)
            except:
                log_this("Error opening upath %s" % upath)
                continue

            try:
                udata = json.load(ufd)
            except:
                ufd.close()
                log_this("Error reading json data from file %s" % upath)
                continue

            #log_this("in get_user: udata= %s" % udata)
            if token == udata.get("auth_token", "not-a-valid-token"):
                try:
                    user = udata["name"]
                except:
                    log_this("Error: missing 'name' field in user data file %s, in req.get_user()" % upath)
                break

        log_this("user=%s" % str(user))
        return user

# end of req_class
#######################

# response objects are dictionaries with the following schema:
# { "result" : "success" (RSLT_OK),
#    "data" : <command-specific> }
# { "result" : "fail",
#    "message": "reason for failure" }

def show_env(req, env, full=0):
    env_keys = env.keys()
    env_keys.sort()

    env_filter=["PATH_INFO", "QUERY_STRING", "REQUEST_METHOD", "SCRIPT_NAME"]
    req.html.append("Here is the environment:")
    req.html.append("<ul>")
    for key in env_keys:
        if full or key in env_filter:
            req.html.append("<li>%s=%s" % (key, env[key]))
    req.html.append("</ul>")

def log_env(req):
    env_keys = req.environ.keys()
    env_keys.sort()

    log_this("Here is the environment:")
    for key in env_keys:
        log_this("%s=%s" % (key, req.environ[key]))

def get_timestamp():
    t = time.time()
    tfrac = int((t - int(t))*100)
    timestamp = time.strftime("%Y-%m-%d_%H:%M:%S.") + "%02d" % tfrac
    return timestamp

def save_file(req, file_field, upload_dir):
    # some debugging...
    F = RSLT_FAIL
    msg = ""

    #msg += "req.form=\n"
    #for k in req.form.keys():
    #   msg += "%s: %s\n" % (k, req.form[k])

    if not req.form.has_key(file_field):
        return F, msg+"Form is missing key %s\n" % file_field, ""

    fileitem = req.form[file_field]
    if not fileitem.file:
        return F, msg+"fileitem has no attribute 'file'\n", ""

    if not fileitem.filename:
        return F, msg+"fileitem has no attribute 'filename'\n", ""

    filepath = upload_dir + os.sep +  fileitem.filename
    if os.path.exists(filepath):
        return F, msg+"Already have a file %s. Cannot proceed.\n" % fileitem.filename, ""

    fout = open(filepath, 'wb')
    while 1:
        chunk = fileitem.file.read(100000)
        if not chunk:
            break
        fout.write(chunk)
    fout.close()
    msg += "File '%s' uploaded successfully!\n" % fileitem.filename
    return RSLT_OK, msg, filepath

# this routine is the old-style action API, and is deprecated
def do_put_object(req, obj_type):
    data_dir = req.config.data_dir + os.sep + obj_type + "s"
    result = RSLT_OK
    msg = ""

    # convert form (cgi.fieldStorage) to dictionary
    try:
        obj_name = req.form["name"].value
    except:
        msg += "Error: missing %s name in form data" % obj_type
        req.send_response(req, RSLT_FAIL, msg)
        return

    obj_dict = {}
    for k in req.form.keys():
        obj_dict[k] = req.form[k].value

    # sanity check the submitted data
    for field in required_put_fields[obj_type]:
        try:
            value = obj_dict[field]
        except:
            result = RSLT_FAIL
            msg += "Error: missing required field '%s' in form data" % field
            break

        # FIXTHIS - for cross references (board, resource), check that these
        # are registered with the server
        # here is an example:
        # see if a referenced board is registered with the server
        #if field.startswith("board") or field.endswith("board"):
        #    board_filename = "board-%s.json" % (value)
        #    board_data_dir = req.config.data_dir + os.sep + "boards"
        #    board_path = board_data_dir + os.sep + board_filename
        #
        #    if not os.path.isfile(board_path):
        #        result = RSLT_FAIL
        #        msg += "Error: No matching board '%s' registered with server (from field '%s')" % (value, field)
        #        break

    if result != RSLT_OK:
        req.send_response(req, result, msg)
        return

    req_result = None
    if obj_type == "request":
        obj_dict["state"] = "pending"
        timestamp = get_timestamp()
        obj_name += "-" + timestamp
        obj_dict["name"] = obj_name

    filename = obj_type + "-" + obj_name
    jfilepath = data_dir + os.sep + filename + ".json"

    # convert to json and save to file
    data = json.dumps(obj_dict, sort_keys=True, indent=4,
            separators=(',', ': '))
    fout = open(jfilepath, "w")
    fout.write(data+'\n')
    fout.close()

    msg += "%s accepted (filename=%s)\n" % (obj_name, filename)

    if obj_type == "request":
        req_result, req_msg = process_request(req, obj_dict)
        msg += "%s\n%s" % (req_result, req_msg)

    req.send_response(req, result, msg)

# define an array with the fields that allowed to be modified
# for each different object type:
allowed_update_fields = {
    "board": ["state", "kernel_version", "reservation"],
    "request": ["state", "start_time", "done_time"],
    "resource": ["state", "reservation", "command"]
    }

# Update board, resource and request objects
def do_update_object(req, obj_type):
    data_dir = req.config.data_dir + os.sep + obj_type + "s"
    msg = ""

    try:
        obj_name = req.form[obj_type].value
    except:
        msg += "Error: can't read %s from form" % obj_type
        req.send_response(RSLT_FAIL, msg)
        return

    filename = obj_type + "-" + obj_name + ".json"
    filepath = data_dir + os.sep + filename
    if not os.path.exists(filepath):
        msg += "Error: filepath %s does not exist" % filepath
        req.send_response(RSLT_FAIL, msg)
        return

    # read requested object file
    fd = open(filepath, "r")
    obj_dict = json.load(fd)
    fd.close()

    # update fields from (cgi.fieldStorage)
    for k in req.form.keys():
        if k in [obj_type, "action"]:
            # skip these
            continue
        if k in allowed_update_fields[obj_type]:
            # FIXTHIS - could check the data input here
            obj_dict[k] = req.form[k].value
        else:
            msg = "Error - can't change field '%s' in %s %s (not allowed)" % \
                    (k, obj_type, obj_name)
            req.send_response(RSLT_FAIL, msg)
            return

    # put dictionary back in json format (beautified)
    data = json.dumps(obj_dict, sort_keys=True, indent=4,
            separators=(',', ': '))
    fout = open(filepath, "w")
    fout.write(data+'\n')
    fout.close()

    req.send_response(RSLT_OK, data)

# try matching with simple wildcards (* at start or end of string)
def item_match(pattern, item):
    if pattern=="*":
        return True
    if pattern==item:
        return True
    if pattern.endswith("*") and \
        pattern[:-1] == item[:len(pattern)-1]:
        return True
    if pattern.startswith("*") and \
        pattern[1:] == item[-(len(pattern)-1):]:
        return True
    return False

def do_query_objects(req):
    try:
        obj_type = req.form["obj_type"].value
    except:
        msg = "Error: can't read object type ('obj_type') from form"
        req.send_response(RSLT_FAIL, msg)
        return

    if obj_type not in ["board", "resource", "request"]:
        msg = "Error: unsupported object type '%s' for query" % obj_type
        req.send_response(RSLT_FAIL, msg)
        return

    data_dir = req.config.data_dir + os.sep + obj_type + "s"
    msg = ""

    filelist = os.listdir(data_dir)
    filelist.sort()

    # can query by different fields
    # obj_name is in the filename so we don't need to open the json file
    #   in order to filter by it.
    # other fields are inside the json and requiring opening each file

    try:
        query_obj_name = req.form["name"].value
    except:
        query_obj_name = "*"

    # handle name-based queries
    match_list = []
    for f in filelist:
        prefix = obj_type + "-"
        if f.startswith(obj_type + "-") and f.endswith(".json"):
            file_obj_name = f[len(prefix):-5]
            if not file_obj_name:
                continue
            if not item_match(query_obj_name, file_obj_name):
                continue
            match_list.append(file_obj_name)

    # FIXTHIS - read files and filter by attributes
    # particularly filter on 'state'

    for obj_name in match_list:
       msg += obj_name+"\n"

    req.send_response(RSLT_OK, msg)


def old_do_query_requests(req):
    #log_this("in do_query_requests")
    req_data_dir = req.config.data_dir + os.sep + "requests"
    msg = ""

    filelist = os.listdir(req_data_dir)
    filelist.sort()

    # can query by different fields, some in the name and some inside
    # the json

    try:
        query_host = req.form["host"].value
    except:
        query_host = "*"

    try:
        query_board = req.form["board"].value
    except:
        query_board = "*"

    # handle host and board-based queries
    match_list = []
    for f in filelist:
        if f.startswith("request-") and f.endswith("json"):
            host_and_board = f[31:-5]
            if not host_and_board:
                continue
            if not item_match(query_host, host_and_board.split(":")[0]):
                continue
            if not item_match(query_board, host_and_board.split(":")[1]):
                continue
            match_list.append(f)

    # read files and filter by attributes
    # (particularly filter on 'state')
    if match_list:
        # read the first file to get the list of possible attributes
        f = match_list[0]
        with open(req_data_dir + os.sep + f) as jfd:
            data = json.load(jfd)
            # get a list of valid attributes
            fields = data.keys()

            # get rid of fields already processed
            fields.remove("host")
            fields.remove("board")

        # check the form for query attributes
        # if they have the same name as a valid field, then add to list
        query_fields={}
        for field in fields:
            try:
                query_fields[field] = req.form[field].value
            except:
                pass

        # if more to query by, then go through files, preserving matches
        if query_fields:
            ml_tmp = []
            for f in match_list:
                drop = False
                with open(req_data_dir + os.sep + f) as jfd:
                    data = json.load(jfd)
                    for field, pattern in query_fields.items():
                        if not item_match(pattern, str(data[field])):
                            drop = True
                if not drop:
                    ml_tmp.append(f)
            match_list = ml_tmp

    for f in match_list:
        # remove .json extension from request filename, to get the req_id
        req_id = f[:-5]
        msg += req_id+"\n"

    req.send_response(RSLT_OK, msg)

# FIXTHIS - could do get_next_request (with wildcards) to save a query
def do_get_request(req):
    req_data_dir = req.config.data_dir + os.sep + "requests"
    msg = ""

    # handle host and target-based queries
    msg += "In lcserver.py:get_request\n"
    try:
        request_id = req.form["request_id"].value
    except:
        msg += "Error: can't read request_id from form"
        req.send_response(RSLT_FAIL, msg)
        return

    filename = request_id + ".json"
    filepath = req_data_dir + os.sep + filename
    if not os.path.exists(filepath):
        msg += "Error: filepath %s does not exist" % filepath
        req.send_response(RSLT_FAIL, msg)
        return

    # read requested file
    request_fd = open(filepath, "r")
    mydict = json.load(request_fd)

    # beautify the data, for now
    data = json.dumps(mydict, sort_keys=True, indent=4, separators=(',', ': '))
    req.send_response(RSLT_OK, data)

def do_remove_object(req, obj_type):
    data_dir = req.config.data_dir + os.sep + obj_type + "s"
    msg = ""

    try:
        obj_name = req.form[obj_type].value
    except:
        msg += "Error: can't read '%s' from form" % obj_type
        req.send_response(RSLT_FAIL, msg)
        return

    filename = obj_name + ".json"
    filepath = data_dir + os.sep + filename
    if not os.path.exists(filepath):
        msg += "Error: filepath %s does not exist" % filepath
        req.send_response(RSLT_FAIL, msg)
        return

    # FIXTHIS - should check permissions here
    # only original-submitter and resource-host are allowed to remove
    os.remove(filepath)

    msg += "%s %s was removed" % (obj_type, obj_name)
    req.send_response(RSLT_OK, msg)

def file_list_html(req, file_type, subdir, extension):
    if file_type == "files":
        src_dir = req.config.files_dir + os.sep + subdir
    elif file_type == "data":
        src_dir = req.config.data_dir + os.sep + subdir
    elif file_type == "page":
        src_dir = req.config.page_dir
    else:
        raise ValueError("cannot list files for file_type %s" % file_type)

    full_dirlist = os.listdir(src_dir)
    full_dirlist.sort()

    # filter list to only ones with requested extension
    filelist = []
    for d in full_dirlist:
        if d.endswith(extension):
            filelist.append(d)

    if not filelist:
        return req.html_error("No %s (%s) files found." % (subdir[:-1], extension))

    files_url = "%s/%s/%s/" % (config.files_url_base, file_type, subdir)
    html = "<ul>"
    for item in filelist:
        html += '<li><a href="'+files_url+item+'">' + item + '</a></li>\n'
    html += "</ul>"
    return html

def show_request_table(req):
    src_dir = req.config.data_dir + os.sep + "requests"

    full_dirlist = os.listdir(src_dir)
    full_dirlist.sort()

    # filter list to only request....json files
    filelist = []
    for f in full_dirlist:
        if f.startswith("request") and f.endswith(".json"):
            filelist.append(f)

    if not filelist:
        return req.html_error("No request files found.")

    files_url = config.files_url_base + "/data/requests/"
    html = """<table border="1" cellpadding="2">
  <tr>
    <th>Request</th>
    <th>State</th>
    <th>Requestor</th>
    <th>Host</th>
    <th>Board</th>
    <th>Test</th>
    <th>Run (results)</th>
  </tr>
"""
    for item in filelist:
        request_fd = open(src_dir+os.sep + item, "r")
        req_dict = json.load(request_fd)
        request_fd.close()

        # add data, in case it's missing
        try:
            run_id = req_dict["run_id"]
        except:
            req_dict["run_id"] = "Not available"

        html += '  <tr>\n'
        html += '    <td><a href="'+files_url+item+'">' + item + '</a></td>\n'
        for attr in ["state", "requestor", "host", "board", "test_name",
                "run_id"]:
            html += '    <td>%s</td>\n' % req_dict[attr]
        html += '  </tr>\n'
    html += "</table>"
    req.html.append(html)

# NOTE: we're inside a table cell here
def show_board_info(req, bmap):
    # list of connected resources
    # FIXTHIS - what to show here:
    # status, action button for reboot
    # reservations
    req.html.append("<h3>Resources</h3>\n<ul>")
    pc = bmap.get("power_controller", "")
    resource_shown = False
    if pc:
        req.html.append("<li>Power controller: %s</li>\n" % pc)
        resource_shown = True
    if not resource_shown:
        req.html.append("<li><i>No connected resources found!</i></li>\n")
    req.html.append("</ul>\n")

    req.html.append("<h3>Status</h3>\n<ul>")

    reservation = bmap.get("reservation", "None")
    req.html.append("<li>Reservation: %s</li>" % reservation)

    # show power status
    if pc:
       (result, msg) = get_power_status(req, bmap)
       if result == RSLT_OK:
           power_status = msg
       else:
           power_status = req.html_error(msg)
    else:
       power_status = "Unknown"
    req.html.append("<li>Power Status: %s</li>\n" % power_status)

    req.html.append("</ul>\n")

    req.html.append("<h3>Actions</h3>\n<ul>\n")
    if pc:
        reboot_link = req.config.url_base + "/api/devices/%s/power/reboot" % (bmap["name"])
        req.html.append("""
<form method="get" action=%s>
<input type="submit" name="button" value="Reboot">
</form>
""" % reboot_link)
    req.html.append("</ul>")

# returns (RSLT_OK, status|RSLT_FAIL, message)
# status can be one of: "ON", "OFF", "UNKNOWN"
def get_power_status(req, bmap):
    pdu_map = get_connected_resource(req, bmap, "power_controller")
    if not pdu_map:
        msg = "Board %s has no connected power_controller resource" % bmap["name"]
        return (RSLT_FAIL, msg)

    # lookup command to execute in resource_map
    if "status_cmd" not in pdu_map:
        msg = "Resource '%s' does not have status_cmd attribute, cannot execute" % pdu_map["name"]
        return (RSLT_FAIL, msg)

    cmd_str = pdu_map["status_cmd"]
    rcode, status = getstatusoutput(cmd_str)
    if rcode:
        msg = "Result of power status operation on board %s = %d\n" % (bmap["name"], rcode)
        msg += "command output='%s'" % status
        return (RSLT_FAIL, msg)

    # FIXTHIS - translate result here, if needed

    return (RSLT_OK, status)

# show the web ui for boards on this machine
def show_boards(req):
    req.html.append("<H1>Boards</h1>")
    boards = get_object_list(req, "board")

    # show a table of attributes
    req.html.append('<table class="board_table" border="1" style="border-collapse: collapse; padding: 5px" >\n<tr>\n')
    req.html.append("  <th>Picture</th><th>Name</th><th>Description</th><th>Data and Actions</th>\n</tr>\n")
    for board in boards:
        req.html.append("<tr>\n")
        bmap = get_object_map(req, "board", board)
        req.html.append('  <td valign="middle" style="padding: 5px"><i>No picture</i></td>\n')
        req.html.append('  <td valign="top" align="center" style="padding: 5px"><h3>%(name)s</h3>(in %(host)s)</td>\n' % bmap)
        req.html.append('  <td valign="top" style="padding: 5px">%(description)s</td>\n' % bmap)
        # FIXTHIS - what to show here:
        # status, action for on/off/reboot
        # list of connected resources
        # reservations
        req.html.append('  <td style="padding: 10px">')
        show_board_info(req, bmap)
        req.html.append("</td>\n")
        req.html.append("</tr>\n")

    req.html.append("</table>")
    req.show_footer()

def show_users(req):
    req.html.append("<H1>Users</h1>")
    users = get_object_list(req, "user")

    # show a table of attributes
    req.html.append('<table class="user_table" border="1" style="border-collapse: collapse; padding: 5px" >\n<tr>\n')
    req.html.append("  <th>Name</th><th>Reservations</th><th>Last access</th>\n</tr>\n")
    for user in users:
        req.html.append("<tr>\n")
        umap = get_object_map(req, "user", user)
        req.html.append('  <td valign="top" align="center" style="padding: 5px"><h3>%(name)s</h3></td>\n' % umap)
        req.html.append('  <td valign="top" style="padding: 5px"><i>Not implemented yet</i></td>\n')
        req.html.append('  <td valign="top" style="padding: 5px"><i>Not implemented yet</i></td>\n')
        req.html.append("</tr>\n")

    req.html.append("</table>")
    req.show_footer()


# show the web ui for objects on the server
# this is the main human interface to the server
def do_show(req):
    req.show_header("Lab Control objects")
    #log_this("in do_show, req.page_name='%s'\n" % req.page_name)
    #req.html.append("req.page_name='%s' <br><br>" % req.page_name)

    if req.page_name not in ["boards", "resources", "users", "requests", "logs", "main"]:
        # FIXTHIS - check for object name here, and show individual object
        #   status and control interface
        # it should be in req.obj_path
        title = "Error - unknown object type '%s'" % req.page_name
        req.add_to_message(title)
    else:
        if req.page_name=="boards":
            show_boards(req)
        elif req.page_name == "users":
            show_users(req)
        elif req.page_name == "resources":
            req.html.append("<H1>List of resources</h1>")
            req.html.append(file_list_html(req, "data", "resources", ".json"))
        elif req.page_name == "requests":
            req.html.append("<H1>Table of requests</H1>")
            show_request_table(req)
        elif req.page_name == "logs":
            req.html.append("<H1>Table of logs</H1>")
            req.html.append(file_list_html(req, "files", "logs", ".txt"))

    if req.page_name != "main":
        req.html.append("<br><hr>")

    req.html.append("<H1>Lab Control objects on this server</h1>")
    req.html.append("""
Here are links to the different Lab Control objects:<br>
<ul>
<li><a href="%(url_base)s/boards">Boards</a></li>
<li><a href="%(url_base)s/resources">Resources</a></li>
<li><a href="%(url_base)s/users">Users</a></li>
<li><a href="%(url_base)s/requests">Requests</a></li>
<li><a href="%(url_base)s/logs">Logs</a></li>
</ul>
<hr>
""" % req.config )

    req.html.append("""<a href="%(url_base)s/raw">Show raw objects</a><br>\n""" % req.config)
    req.html.append("""<a href="%(url_base)s">Back to home page</a>""" % req.config)

    req.show_footer()

# show raw objects
#  if page_name is "main", show a list of different object types
def do_raw(req):
    req.show_header("Lab Control Raw objects")
    log_this("in do_raw, req.page_name='%s'\n" % req.page_name)
    req.html.append("req.page_name='%s' <br><br>" % req.page_name)

    if req.page_name not in ["boards", "resources", "users", "requests", "logs", "main"]:
        title = "Error - unknown object type '%s'" % req.page_name
        req.add_to_message(title)
    else:
        if req.page_name=="boards":
            req.html.append("<H1>List of boards</h1>")
            req.html.append(file_list_html(req, "data", "boards", ".json"))
        elif req.page_name == "resources":
            req.html.append("<H1>List of resources</h1>")
            req.html.append(file_list_html(req, "data", "resources", ".json"))
        elif req.page_name == "users":
            req.html.append("<H1>List of users</h1>")
            req.html.append(file_list_html(req, "data", "users", ".json"))
        elif req.page_name == "requests":
            req.html.append("<H1>Table of requests</H1>")
            show_request_table(req)
        elif req.page_name == "logs":
            req.html.append("<H1>Table of logs</H1>")
            req.html.append(file_list_html(req, "files", "logs", ".txt"))

    if req.page_name != "main":
        req.html.append("<br><hr>")

    req.html.append("<H1>Lab Control raw objects on this server</h1>")
    req.html.append("""
Here are links to the different Lab Control objects:<br>
<ul>
<li><a href="%(url_base)s/raw/boards">Boards</a></li>
<li><a href="%(url_base)s/raw/resources">Resources</a></li>
<li><a href="%(url_base)s/raw/users">Users</a></li>
<li><a href="%(url_base)s/raw/requests">Requests</a></li>
<li><a href="%(url_base)s/raw/logs">Logs</a></li>
</ul>
<hr>
""" % req.config )

    req.html.append("""<a href="%(url_base)s">Back to home page</a>""" % req.config)

    req.show_footer()

# get a list of items of the indicated object type
# (by scanning the data/{obj_type}s directory, and
# parsing the filenames)
# returns a list of strings with the item names
def get_object_list(req, obj_type):
    data_dir = req.config.data_dir + os.sep + obj_type + "s"
    obj_list = []

    filelist = os.listdir(data_dir)
    prefix = obj_type+"-"
    for f in filelist:
        if f.startswith(prefix) and f.endswith(".json"):
            # remove board- and '.json' to get the board_name
            obj_name = f[len(prefix):-5]
            obj_list.append(obj_name)

    obj_list.sort()
    return obj_list

# supported api actions by path:
# devices = list boards
# devices/{board} = show board data (json file data)
# devices/{board}/status = show board status
# devices/{board}/power/reboot = reboot board
# resources = list resources
# resources/{resource} = show resource data (json file data)

def return_api_object_list(req, obj_type):
    obj_list = get_object_list(req, obj_type)
    req.send_api_list_response(obj_list)

# read data from json file (from data/{obj_type}s/{obj_type}-{obj_name}.json)
# log any errors encountered
def get_object_data(req, obj_type, obj_name):
    filename = obj_type + "-" + obj_name + ".json"
    file_path = "%s/%ss/%s-%s.json" %  (req.config.data_dir, obj_type, obj_type, obj_name)

    if not os.path.isfile(file_path):
        msg = "%s object '%s' in not recognized by the server" % (obj_type, obj_name)
        msg += "- file_path was '%s'" % file_path
        log_this(msg)
        return {}

    data = ""
    try:
        data = open(file_path, "r").read()
    except:
        msg = "Could not retrieve information for %s '%s'" % (obj_type, obj_name)
        msg += "- file_path was '%s'" % file_path
        log_this(msg)
        return {}

    return data

# get object data from file, return api response on error
def get_api_object_data(req, obj_type, obj_name):
    filename = obj_type + "-" + obj_name + ".json"
    file_path = "%s/%ss/%s-%s.json" %  (req.config.data_dir, obj_type, obj_type, obj_name)

    if not os.path.isfile(file_path):
        msg = "%s object '%s' in not recognized by the server" % (obj_type, obj_name)
        msg += "- file_path was '%s'" % file_path
        req.send_api_response_msg(RSLT_FAIL, msg)
        return {}

    data = ""
    try:
        data = open(file_path, "r").read()
    except:
        msg = "Could not retrieve information for %s '%s'" % (obj_type, obj_name)
        msg += "- file_path was '%s'" % file_path
        req.send_api_response_msg(RSLT_FAIL, msg)
        return {}

    return data

# return the list of boards that I have reserved
# (that are assigned to me)
def return_my_board_list(req):
    user = req.get_user()

    boards = get_object_list(req, "board")
    my_boards =  []
    for board in boards:
        board_map = get_object_map(req, "board", board)
        if not board_map:
            continue
        assigned_to = board_map.get("AssignedTo", "nobody")
        if user == assigned_to:
            my_boards.append(board)

    req.send_api_list_response(my_boards)

# return python data structure from json file
#  (from data/{obj_type}s/{obj_type}-{obj_name}.json)
def get_object_map(req, obj_type, obj_name):
    data = get_object_data(req, obj_type, obj_name)
    if not data:
        return {}
    try:
        obj_map = json.loads(data)
    except:
        msg = "Invalid json detected in %s '%s'" % (obj_type, obj_name)
        msg += "\njson='%s'" % data
        log_this(msg)
        return {}

    return obj_map

# return python data structure from json file
#  (from data/{obj_type}s/{obj_type}-{obj_name}.json)
def get_api_object_map(req, obj_type, obj_name):
    data = get_api_object_data(req, obj_type, obj_name)
    if not data:
        return {}
    try:
        obj_map = json.loads(data)
    except:
        msg = "Invalid json detected in %s '%s'" % (obj_type, obj_name)
        msg += "\njson='%s'" % data

        req.send_api_response_msg(RSLT_FAIL, msg)
        return {}

    return obj_map

def save_object_data(req, obj_type, obj_name, obj_data):
    filename = obj_type + "-" + obj_name + ".json"
    file_path = "%s/%ss/%s-%s.json" %  (req.config.data_dir, obj_type, obj_type, obj_name)

    #log_this("in save_object_data: obj_data=%s" % obj_data)

    json_data = json.dumps(obj_data, sort_keys=True, indent=4,
        separators=(',', ': '))

    try:
        ofd = open(file_path, "w")
        ofd.write(json_data)
        ofd.close()
    except:
        log_this("Error: cannot write data to file %s" % file_path)

    return

def get_connected_resource(req, board_map, resource_type):
    # look up connected resource type in board map
    resource = board_map.get(resource_type, None)
    if not resource:
        msg = "Could not find a %s resource connected to board '%s'" % (resource_type, board)
        req.send_response(RSLT_FAIL, msg)
        return None

    resource_map = get_object_map(req, "resource", resource)
    return resource_map

def return_api_object_data(req, obj_type, obj_name):
    # do default action for an object - return json file data (as a string)
    data = get_api_object_map(req, obj_type, obj_name)
    if not data:
        return

    # perform any data transformations required for compliance with spec
    # FIXTHIS - should manage this schema with TimeSys

    req.send_api_response(RSLT_OK, data)

# execute a resource command
# returns a tuple of (result, string)
def exec_command(req, board_map, resource_map, res_cmd):
    # lookup command to execute in resource_map
    res_cmd_str = res_cmd + "_cmd"
    if res_cmd_str not in resource_map:
        msg = "Resource '%s' does not have %s attribute, cannot execute" % (resource["name"], res_cmd_str)
        return (RSLT_FAIL, msg)

    cmd_str = resource_map[res_cmd_str]

    # FIXTHIS - do substitution of variables from board_map and resource_map
    # into the cmd_str, like the following:
    # if has_fmt(cmd_str):
    #   cmd_str = cmd_str % board_map
    #
    # This allows a command to refer to a variable defined in the board
    # or resource data

    rcode, result = getstatusoutput(cmd_str)
    if rcode:
        msg = "Result of %s operation on resource %s = %d" % (res_cmd, resource["name"], rcode)
        msg += "command output='%s'" % result
        return (RSLT_FAIL, msg)

    return (RSLT_OK, result)

# execute a resource command
def return_exec_command(req, board_map, resource_map, res_cmd):
    (result, msg) = exec_command(req, board_map, resource_map, res_cmd)
    req.send_api_response_msg(result, msg)

# rest is a list of the rest of the path
# supported actions are: get_resource, power, assign, release, run
def return_api_board_action(req, board, action, rest):
    log_this("rest=%s" % rest)
    boards = get_object_list(req, "board")
    if board not in boards:
        msg = "Could not find board '%s' registered with server" % board
        req.send_api_response_msg(RSLT_FAIL, msg)
        return

    board_map = get_object_map(req, "board", board)
    if not board_map:
        msg = "Problem loading data for board '%s'" % board
        req.send_api_response_msg(RSLT_FAIL, msg)
        return

    if action == "get_resource":
        res_type = rest[0]
        del(rest[0])
        if res_type not in ["power_controller", "power_measurement", "serial",
                "canbus"]:
            msg = "Error: invalid resource type '%s'" % res_type
            req.send_api_response_msg(RSLT_FAIL, msg)
            return

        if not rest:
            resource = board_map.get(res_type, None)
            if not resource:
                msg = "Error: no resource type '%s' associated with board '%s'" % \
                         (res_type, board)
                req.send_api_response_msg(RSLT_FAIL, msg)
                return

            req.send_api_response(RSLT_OK, { "data": resource })
            return

        if rest:
            feature = urllib.unquote(rest[0])
            resource, msg = find_resource(req, board, feature)
            if not resource:
                req.send_api_response_msg(RSLT_FAIL, msg)
                return

            req.send_api_response(RSLT_OK, { "data": resource })
            return
            # match it to board_endpoint in resource file

    if action == "power":
        pdu_map = get_connected_resource(req, board_map, "power_controller")
        if not pdu_map:
            msg = "No power controller resource found for board %s" % board
            req.send_api_response_msg(RSLT_FAIL,  msg)
            return

        if not rest or rest[0] == "status":
            (result, msg) = get_power_status(req, board_map)
            log_this("power status result=%s,%s" % (result, msg))
            if result==RSLT_OK:
                req.send_api_response(result, {"data": msg})
            else:
                req.send_api_response_msg(result, msg)
            return
        elif rest[0] in ["on", "off", "reboot"]:
            return_exec_command(req, board_map, pdu_map, rest[0])
            return
        else:
            msg = "power action '%s' not supported" % rest[0]
            req.send_api_response_msg(RSLT_FAIL, msg)
            return
    elif action == "assign":
        # get current user, and add reservation for board to user
        user = req.get_user()
        assigned_to = board_map.get("AssignedTo", "nobody")
        if assigned_to != "nobody":
            if user == assigned_to:
                msg = "Device is already assigned to you"
            else:
                msg = "Device is already assigned to %s" % assigned_to
            req.send_api_response_msg(RSLT_FAIL, msg)
            return

        if user and user != "nobody":
            board_map["AssignedTo"] = user
        else:
            msg = "Cannot determine user for operation"
            req.send_api_response_msg(RSLT_FAIL, msg)
            return

        # save data back to json file
        save_object_data(req, "board", board, board_map)

        req.send_api_response(RSLT_OK)
        return

    elif action == "release":
        # get current user, and remove reservation for board
        user = req.get_user()
        assigned_to = board_map.get("AssignedTo", "nobody")
        if assigned_to == "nobody":
            msg = "Device is already free and available for allocation."
            req.send_api_response_msg(RSLT_FAIL, msg)
            return

        if not user or user == "nobody":
            msg = "Cannot determine user for operation"
            req.send_api_response_msg(RSLT_FAIL, msg)
            return

        if rest and rest[0] == "force":
            force = True
        else:
            force = False

        if not force:
            if user != assigned_to:
                msg = "Device is not assigned to you. It is assigned to '%s'.\nCannot release it. (try using 'force' option)" % assigned_to
                req.send_api_response_msg(RSLT_FAIL, msg)
                return

        board_map["AssignedTo"] = "nobody"

        # save data back to json file
        save_object_data(req, "board", board, board_map)

        req.send_api_response(RSLT_OK)
        return

    elif action == "run":
        # check that user has board reserved
        user = req.get_user()
        assigned_to = board_map.get("AssignedTo", "nobody")

        if user != assigned_to:
            msg = "Device is not assigned to you. It is assigned to '%s'.\nCannot run command." % assigned_to
            req.send_api_response_msg(RSLT_FAIL, msg)
            return

        # This seems optimistic - maybe add some error handling here
        command_from_user = json.loads(req.form.value)["command"]

        d = copy.deepcopy(board_map)
        run_cmd = board_map.get("run_cmd", None)
        if not run_cmd:
            msg = "Device '%s' is not configured to run commands" % board
            req.send_api_response_msg(RSLT_FAIL, msg)
            return

        d["command"] = command_from_user
        cmd = run_cmd % d

        log_this("About to run_command('%s')" % cmd)

        lines, rcode, msg = run_command(cmd)
        if msg:
            req.send_api_response_msg(RSLT_FAIL, msg)
            return

        data = { "return_code": rcode, "data": lines }
        jdata = json.dumps(data)
        log_this("jdata='%s'" % jdata)

        req.send_api_response(RSLT_OK, { "data": data } )
        return

    msg = "action '%s' not supported (rest='%s')" % (action, rest)
    req.send_api_response_msg(RSLT_FAIL, msg)

CAPTURE_LOG_FILENAME_FMT="/tmp/capture-log-%s.txt"
capture_dir="/tmp"
capture_prefix="capture-log-"
capture_suffix=".txt"
CAPTURE_PID_FILENAME_FMT="/tmp/capture-%s.pid"

data_dir="/tmp"
data_prefix="data-file-"
data_suffix=".data"

# return pid of command
# only execute a single-line command, for now
def new_exec_command(cmd):
    from subprocess import Popen, PIPE, STDOUT


    exec_args = shlex.split(cmd)

    # FIXTHIS - add more stuff here

    try:
        proc = Popen(exec_args, stdin=PIPE, stdout=PIPE, stderr=PIPE, close_fds=True)
    except subprocess.CalledProcessError as e:
        msg = "Can't run command '%s' in exec_command" % cmd
        return (0, msg)
    except OSError as error:
        msg = error + " trying to execute command '%s'" % cmd
        return (0, msg)

    pid = proc.pid
    return (pid, "OK")

def run_timeout(proc):
    log_this("run_timeout fired! - killing process %s" % proc.pid)
    proc.kill()


# execute a single-line command, and return:
# lines, return_code, reason
# where lines is the command output, and return_code is the exit code
# of the command.  On failure, reason is non-empty and contains
# a description of the problem.
def run_command(cmd):
    from subprocess import Popen, PIPE, STDOUT

    exec_args = shlex.split(cmd)

    # FIXTHIS - add more stuff here

    try:
        proc = Popen(exec_args, stdin=PIPE, stdout=PIPE, stderr=PIPE, close_fds=True)
    except subprocess.CalledProcessError as e:
        msg = "Can't run command '%s' in exec_command" % cmd
        return (0, msg)
    except OSError as error:
        msg = error + " trying to execute command '%s'" % cmd
        return (0, msg)

    # don't allow command to run for more than 60 seconds
    timer = threading.Timer(60.0, run_timeout, [proc])
    timer.start()

    # we may want to have a different interface here, that allows
    # starting the command, and returning partial data after 10 seconds
    # followed by periodic queries from the client to get additional data
    # before finally yeilding the final data and return code
    # we would need a file buffer and pid system, like for the capture code.

    pid = proc.pid
    try:
        output, errs = proc.communicate()
    except:
        proc.kill()
        output, errs = proc.communicate()

    timer.cancel()

    # FIXTHIS - run_command discards error output

    rcode = proc.returncode
    log_this("rcode=%s" % rcode)

    log_this("output='%s'" % output)
    # keep the newlines on the lines
    lines = output.splitlines(True)
    #lines = output.split("\n")
    return (lines, rcode, None)

# returns non-empty reason string on failure
def set_config(req, action, resource_map, config_map, rest):
    resource = resource_map["name"]
    config_cmd = resource_map.get("config_cmd", "")
    if not config_cmd:
        return "Could not find 'config_cmd' for resource resource %s" %  resource

    dlog_this("config_cmd=" + config_cmd)

    allowed_config_items=["baud_rate"]

    new_resource_map = copy.deepcopy(resource_map)
    # only copy allowed items from config_map
    for key, value in config_map.items():
        if key in allowed_config_items:
            new_resource_map[key] = value

    cmd_str = config_cmd % new_resource_map
    dlog_this("(interpolated) cmd_str='%s'" + cmd_str)
    rcode, result = getstatusoutput(cmd_str)
    if rcode:
        msg = "Result of set-config operation on resource %s = %d\n" % (resource, rcode)

        # Apparently, 'stty' error output uses some high unicode code points,
        # (outside the ascii range), which cause an exception if you don't
        # decode them. (ie, they get auto-decoded as ascii)
        # Have I told you, lately, how much I hate python unicode handling??
        output = result.decode('utf8', errors='ignore')
        msg += "command output (decoded)='" + output + "'"
        return msg

    # write out updated resource_map
    save_object_data(req, "resource", resource, new_resource_map)

    return None


# returns token, reason
# on error, token is None or empty and reason is a string with an error
# message.  The error message should start with "Error: "
def start_capture(req, action, resource_map, rest):
    # look up capture_cmd in resource_map, and call it
    resource = resource_map["name"]
    capture_cmd = resource_map.get("capture_cmd")

    log_this("capture_cmd=" + capture_cmd)

    # generate the logfile path, and hand  to the capture_cmd
    # FIXTHIS - use a hardcoded logfile and pidfile for now
    token = "1234"
    fd, logpath = tempfile.mkstemp(capture_suffix, capture_prefix, capture_dir)
    os.close(fd)
    os.remove(logpath)
    filename = os.path.basename(logpath)
    token = filename[len(capture_prefix):-len(capture_suffix)]

    pidfile = CAPTURE_PID_FILENAME_FMT % token

    if os.path.exists(pidfile):
        # FIXTHIS - in start_capture, check and see if process is still running
        # if not, just remove pidfile, and continue
        return ("", "Capture is already running for resource %s" % resource)

    # do string interpolation from the data in the resource map
    # (adding the 'logfile' attribute)
    d = copy.deepcopy(resource_map)
    d["logfile"] = logpath

    cmd = capture_cmd % d
    log_this("(interpolated) cmd=" + cmd)

    # save pid in a file, named with the token used earlier
    pid, msg = new_exec_command(cmd)
    if not pid:
        log_this("exec failure: reason=" + msg)
        return ("", msg)

    log_this("capture pid=%d" % pid)
    fd = open(pidfile,"w")
    fd.write(str(pid))
    fd.close()

    return (token, "")

# sends reason on failure
def stop_capture(req, action, resource_map, token, rest):
    resource = resource_map["name"]

    pidfile = CAPTURE_PID_FILENAME_FMT % token

    if not os.path.exists(pidfile):
        return "Cannot find executing capture for %s for resource '%s'" % (action, resource)

    # Could support optional stop_cmd execution here
    # but let's wait on that.
    try:
        fd = open(pidfile, "r")
        pid = int(fd.read().strip())
        fd.close()
    except IOError:
        pid = None

    if not pid:
        return "Cannot find in-progress capture for %s for resource '%s'" % (action, resource)

    try:
        while 1:
            os.kill(pid, signal.SIGTERM)
            time.sleep(0.1)
    except OSError as err:
        err = str(err)
        if err.find("No such process") > 0:
            if os.path.exists(pidfile):
                os.remove(pidfile)

    return None

# returns data, reason
# data is empty on failure, and reason is a string with error message
# otherwise, sends data from capture.  Captured data may be transformed
# from its original format, but in all cases should be sent as json.
def get_captured_data(req, action, resource_map, token, rest):
    resource = resource_map["name"]

    logfile = CAPTURE_LOG_FILENAME_FMT % token

    if not os.path.exists(logfile):
        return (None, "Cannot find capture log for %s token %s for resource '%s'" % (action, token, resource))

    try:
        fd = open(logfile, "r")
        log_data = fd.read()
        fd.close()
    except IOError:
        log_data = None

    if not log_data:
        return (None, "Cannot read capture data for %s for resource '%s'" % (action, resource))

    # convert to json data
    # FIXTHIS - should not use hardcoded re-format operation here, for sdb data
    # should run a conversion command specified by the resource object
    if action == "power_measurement":
        jdata = "[\n"
        for line in log_data.split("\n"):
            if not line:
                continue
            parts = line.split(",")
            try:
                jdata += ' { "timestamp": "%s", "voltage": "%s", "current": "%s" }\n' % (parts[0], float(parts[1])/1000.0, float(parts[2])/1000.0)
            except:
                log_this("Problem converting log_data line for power measurement\nline='%s'" % line)
        jdata += "]"
        return (jdata, "")

    return (log_data, "")

# returns reason on failure, "" on success
def delete_capture(req, res_type, resource_map, token, rest):
    resource = resource_map["name"]

    logfile = CAPTURE_LOG_FILENAME_FMT % token
    if not os.path.exists(logfile):
        return "Cannot delete captured data for resource '%s'" % resource
    os.remove(logfile)
    return ""

def put_data(req, action, resource_map, rest):
    resource = resource_map["name"]
    put_cmd = resource_map.get("put_cmd", "")
    if not put_cmd:
        return "Could not find 'put_cmd' for resource resource %s" %  resource
    dlog_this("put_cmd=" + put_cmd)

    # put data into a file
    data = req.form.value
    log_this("data=%s" % data)

    fd, datapath = tempfile.mkstemp(data_suffix, data_prefix, data_dir)
    os.write(fd, data)
    os.close(fd)

    d = copy.deepcopy(resource_map)
    d["datafile"] = datapath

    cmd_str = put_cmd % d
    dlog_this("(interpolated) cmd_str='%s'" + cmd_str)
    rcode, result = getstatusoutput(cmd_str)
    os.remove(datapath)
    if rcode:
        msg = "Result of put operation on resource %s = %d\n" % (resource, rcode)
        output = result.decode('utf8', errors='ignore')
        msg += "command output (decoded)='" + output + "'"
        return msg

    return None


# rest is a list of the rest of the path
# support actions are: get_resource, power
def return_api_resource_action(req, resource, res_type, rest):
    resources = get_object_list(req, "resource")
    if resource not in resources:
        msg = "Could not find resource '%s' registered with server" % resource
        req.send_api_response_msg(RSLT_FAIL, msg)
        return

    resource_map = get_object_map(req, "resource", resource)
    if not resource_map:
        msg = "Problem loading data for resource '%s'" % resource
        req.send_api_response_msg(RSLT_FAIL, msg)
        return

    operation = rest[0]
    del(rest[0])

    if res_type == "serial" and operation == "set-config":
        config_data = req.form.value
        log_this("config_data=%s" % config_data)

        config_map = json.loads(config_data)
        dlog_this("config_map=%s" % config_map)

        msg = set_config(req, res_type, resource_map, config_map, rest)
        if msg:
            req.send_api_response_msg(RSLT_FAIL, msg)
        else:
            req.send_api_response(RSLT_OK)
        return

    if res_type in ["power_measurement", "serial"]:
        if operation in ["stop_capture", "get-data", "delete"]:
            try:
                token = rest[0]
            except IndexError:
                msg = "Missing token for %s operation" % res_type
                req.send_api_response_msg(RSLT_FAIL, msg)
                return

        if operation == "start_capture":
            token, reason = start_capture(req, res_type, resource_map, rest[1:])
            if not token:
                req.send_api_response_msg(RSLT_FAIL, reason)
                return
            req.send_api_response(RSLT_OK, { "data": token } )
            return
        elif operation == "stop_capture":
            reason = stop_capture(req, res_type, resource_map, token, rest[2:])
            if reason:
                req.send_api_response_msg(RSLT_FAIL, reason)
                return
            req.send_api_response(RSLT_OK)
            return
        elif operation == "get-data":
            data, reason = get_captured_data(req, res_type, resource_map, token, rest[2:])
            if reason:
                req.send_api_response_msg(RSLT_FAIL, reason)
                return
            req.send_api_response(RSLT_OK, { "data": data } )
            return
        elif operation == "delete":
            reason = delete_capture(req, res_type, resource_map, token, rest[2:])
            if reason:
                req.send_api_response_msg(RSLT_FAIL, reason)
                return
            req.send_api_response(RSLT_OK)
            return
        elif operation == "put-data":
            reason = put_data(req, res_type, resource_map, rest)
            if reason:
                req.send_api_response_msg(RSLT_FAIL, reason)
                return
            req.send_api_response(RSLT_OK)
            return
        else:
            msg = "operation '%s' not supported for %s resource" % (operation, res_type)
            req.send_api_response_msg(RSLT_FAIL, msg)
            return


    msg = "resource type '%s' not supported (rest='%s')" % (res_type, rest)
    req.send_api_response_msg(RSLT_FAIL, msg)

# returns token, reason - where token is non-empty on success
def authenticate_user(req, user, password):
    # scan user files for matching user
    user_dir = req.config.data_dir + "/users"
    try:
        user_files = os.listdir( user_dir )
    except:
        msg = "Error: could not read user files from " + user_dir
        log_this(msg)
        return None, msg

    for ufile in user_files:
        if not ufile.startswith("user-"):
            continue
        upath = user_dir + "/" + ufile
        try:
            ufd = open(upath)
        except:
            log_this("Error opening upath %s" % upath)
            continue

        try:
            udata = json.load(ufd)
        except:
            ufd.close()
            log_this("Error reading json data from file %s" % upath)
            continue

        #log_this("in get_user: udata= %s" % udata)
        try:
            user_name = udata["name"]
        except:
            log_this("user file '%s' is missing 'name' field" % upath)
            continue

        if user != user_name:
            continue

        # found a match - check password
        user_password = udata.get("password", "")

        if password == user_password:
            log_this("Authenticated user '%s'" % user)
            token = udata.get("auth_token", "")
            if token:
                return (token, "")
            else:
                return (None, "Invalid token for user '%s' on server" % user)
        else:
            msg = "Password mismatch on login attempt for user '%s'" % user
            log_this(msg)

    return (None, "Authentication for user '%s' failed" % user)

# find a resource that applies to a particular board feature
# returns resource, reason - where resource is non-empty on success
# logs any errors encountered
def find_resource(req, board, feature):
    # scan resource files for a match
    res_dir = req.config.data_dir + "/resources"
    try:
        res_files = os.listdir( res_dir )
    except:
        msg = "Error: could not read resource files from " + res_dir
        log_this(msg)
        return None, msg

    for rfile in res_files:
        dlog_this("checking rfile %s" % rfile)
        if not rfile.startswith("resource-"):
            continue
        rpath = res_dir + "/" + rfile
        try:
            rfd = open(rpath)
        except:
            log_this("Error opening rpath %s" % rpath)
            continue

        try:
            rdata = json.load(rfd)
        except:
            rfd.close()
            log_this("Error reading json data from file %s" % rpath)
            continue

        dlog_this("in find_resource...: rdata= %s" % rdata)
        try:
            rboard = rdata["board"]
        except:
            dlog_this("resource file '%s' is missing 'board' field" % rpath)
            continue

        if board != rboard:
            continue

        # found a match - check board-endpoint against feature string
        board_feature = rdata.get("board_feature", "")

        dlog_this("feature=%s, board_feature=%s" % (feature, board_feature))

        if feature == board_feature:
            return (rdata["name"], None)

    return (None, "No match found for '%s:%s'" % (board, feature))

# api paths are:
#  lc/ebf command -> api path
# list boards, list devices -> api/v0.2/devices/"
# mydevices -> api/v0.2/devices/mine"
# {board} allocate -> api/v0.2/devices/{board}/assign
# {board} release -> api/v0.2/devices/{board}/release"
# {board} release force -> api/v0.2/devices/{board}/release"
# {board} status -> api/v0.2/devices/{board}
# {board} get_resource -> api/v0.2/devices/{board}/get_resource/{resource_type}
# {resource} pm start -> api/v0.2/resources/{resource}/power_measurement/start
# {resource} pm stop -> api/v0.2/resources/{resource}/power_measurement/stop/token
# {resource} pm get-data -> api/v0.2/resources/{resource}/power_measurement/get-data/token
# {resource} pm delete -> api/v0.2/resources/{resource}/power_measurement/delete/token
# {resource} serial start -> api/v0.2/resources/{resource}/serial/start
# {resource} serial stop -> api/v0.2/resources/{resource}/serial/stop/token
# {resource} serial get-data -> api/v0.2/resources/{resource}/serial/get-data/token
# {resource} serial delete -> api/v0.2/resources/{resource}/serial/delete/token
# {resource} serial put-data -> POST api/v0.2/resources/{resource}/serial/put-data

def do_api(req):
    #log_this("in do_api")
    # determine api operation from path
    req_path = req.environ.get("PATH_INFO", "")
    #log_this("TRB: req_path=%s" % req_path)
    path_parts = req_path.split("/")
    # get the path elements after 'api'
    parts = path_parts[path_parts.index("api")+1:]

    #req.show_header("in do_api")
    #req.html.append("parts=%s" % parts)
    log_this("parts=%s" % parts)

    # check API version.  Currently, we only support v0.2
    if parts[0] == "v0.2":
        del(parts[0])
    else:
        req.send_api_response_msg(RSLT_FAIL, "Unsupported api '%s'" % parts[0])

    if not parts:
        msg = "Invalid empty path after /api"
        req.send_api_response_msg(RSLT_FAIL, msg)
        return

    if parts[0] == "token":
        # return auth token for user (on successful authentication)
        #log_this("form.value=%s" % req.form.value)
        data = json.loads(req.form.value)
        try:
            #log_this("data=%s" % data)
            user = data["username"]
            password = data["password"]
        except:
            msg = "Could not parse user/password data from form"
            log_this(msg)
            req.send_api_response_msg(RSLT_FAIL, msg)
            return

        token, reason = authenticate_user(req, user, password)
        if not token:
            req.send_api_response_msg(RSLT_FAIL, reason)
            return

        req.send_api_response(RSLT_OK, { "data": { "token": token } } )
        return

    if parts[0] == "devices":
        if len(parts) == 1:
            # handle /api/devices - list devices
            # list devices (boards)
            return_api_object_list(req, "board")
            return
        else:
            board = parts[1]
            if board == "mine":
                return_my_board_list(req)
                return

            if len(parts) == 2:
                # handle api/devices/{board}
                return_api_object_data(req, "board", board)
                return
            else:
                action = parts[2]
                rest = parts[3:]
                return_api_board_action(req, board, action, rest)
                return
        return
    elif parts[0] == "resources":
        if len(parts) == 1:
            # handle /api/resources - list resources
            # list devices
            return_api_object_list(req, "resource")
            return
        else:
            resource = parts[1]
            if len(parts) == 2:
                # handle api/resource/{resource}
                return_api_object_data(req, "resource", resource)
                return
            else:
                res_type = parts[2]
                rest = parts[3:]
                if res_type in ["power_measurement", "serial", "canbus"]:
                    return_api_resource_action(req, resource, res_type, rest)
                    return

                msg = "Unsupported elements '%s/%s' after /api/resources" % (res_type, "/".join(rest))
                req.send_api_response_msg(RSLT_FAIL, msg)
                return
    elif parts[0] == "requests":
        if len(parts) == 1:
            # handle /api/requests - list requests
            return_api_object_list(req, "request")
            return
        else:
            rest = parts[2:]
            msg = "Unsupported elements '%s' after /api/requests" % ("/".join(rest))
            req.send_api_response_msg(RSLT_FAIL, msg)
            return
    else:
        msg = "Unsupported element '%s' after /api/" % parts[0]
        req.send_api_response_msg(RSLT_FAIL, msg)
        return

def handle_request(environ, req):
    req.environ = environ

    #log_env(req)

    # determine action, if any
    try:
        action = req.form.getfirst("action", "show")
    except TypeError:
        action = "api"

    #reg.add_to_message('action="%s"' % action)
    #log_this('action="%s"' % action)
    req.action = action

    # get page name (last element of path)
    path_info = environ.get("PATH_INFO", "%s/main" % req.config.url_base)
    if path_info.startswith(req.config.url_base):
        obj_path = path_info[len(req.config.url_base):]
    else:
        obj_path = path_info

    page_name = os.path.basename(obj_path)
    if not page_name:
        page_name = "main"
    req.set_page_name(page_name)

    #uncomment these to debug path stuff
    #req.add_to_message("PATH_INFO=%s" % environ.get("PATH_INFO"))
    #req.add_to_message("path_info=%s" % path_info)
    #req.add_to_message("obj_path=%s" % obj_path)
    #req.add_to_message("page_name=%s" % page_name)

    # see if /api is in path
    if obj_path.startswith("/api"):
        action = "api"
        req.api_path = obj_path[4:]

    if obj_path.startswith("/raw"):
        action = "raw"
        req.obj_path = obj_path[4:]

    #req.add_to_message("action=%s" % action)

    # NOTE: uncomment this when you get a 500 error
    #req.show_header('Debug')
    #show_env(req, environ)
    #show_env(req, environ, True)
    log_this("in handle_request: action='%s', page_name='%s'" % (action, page_name))
    #req.add_to_message("in main request loop: action='%s'<br>" % action)

    AUTH_TYPE=req.environ.get("AUTH_TYPE", "none")
    #log_this("in handle_request: AUTH_TYPE='%s'" % AUTH_TYPE)

    # FIXTHIS - look up user by authentication token

    # perform action
    action_list = ["show", "api", "raw",
            "add_board", "add_resource", "put_request",
            "query_objects",
            "get_board", "get_resource", "get_request",
            "remove_board", "remove_resource", "remove_request",
            "update_board", "update_resource", "update_request",
            "put_log", "get_log"]

    # map action names to "do_<action>" functions
    if action in action_list:
        try:
            action_function = globals().get("do_" + action)
        except:
            msg = "Error: unsupported action '%s' (probably missing a do_%s routine)" % (action, action)
            req.send_response(RSLT_FAIL, msg)
            return

        action_function(req)
        return

    req.show_header("LabControl server Error")
    req.html.append(req.html_error("Unknown action '%s'" % action))


def cgi_main():
    form = cgi.FieldStorage()

    req = req_class(config, form)

    try:
        handle_request(os.environ, req)
    except SystemExit:
        pass
    except:
        req.show_header("LabControl Server Error")
        req.html.append('<font color="red">Execution raised by software</font>')

        # show traceback information here:
        req.html.append("<pre>")
        import traceback
        (etype, evalue, etb) = sys.exc_info()
        tb_msg = traceback.format_exc()
        req.html.append("traceback=%s" % tb_msg)
        req.html.append("</pre>")
        log_this("LabControl Server Error")
        log_this("traceback=%s" % tb_msg)

    # output html to stdout
    for line in req.html:
        print(line)

    sys.stdout.flush()

if __name__=="__main__":
    cgi_main()
