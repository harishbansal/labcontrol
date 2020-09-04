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
# The server implements both the human user interace (web pages showing
# the status of the object store), and the computer ReST interface (used
# for sending, modifying and retrieving the data in the store)
#
# To do:
# - queries:
#   - handle regex wildcards instead of just start/end wildcards
# - objects:
#   - support board registration
#   - support resource registration
#     - start with power-controller = ttc
#   - support host registration
#   - support user registration
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
# import these as needed
#import json
#import yaml

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

# this is used for debugging only
def log_this(msg):
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
    def __init__(self, config):
        self.config = config
        self.header_shown = False
        self.message = ""
        self.page_name = ""
        self.page_url = "page_name_not_set_error"
        self.form = None
        # name of page with data being pulled into current page
        # used for things like slideshows or blogs, that get their data
        # (potentially) from another page
        self.processed_page = ""

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
        self.add_to_message("<pre>%s\n</pre>" % tb)

    def show_message(self):
        if self.message:
            print("<h2>lcserver message(s):</h2>")
            print(self.message)

    def show_header(self, title):
        if self.header_shown:
            return

        self.header_shown = True

        self.header = """Content-type: text/html\n\n"""

        # render the header markup
        print(self.header)
        print('<body><h1 align="center">%s</h1>' % title)

    def show_footer(self):
        self.show_message()
        print("</body>")

    def html_error(self, msg):
        return "<font color=red>" + msg + "</font><BR>"


# end of req_class
#######################

def get_env(key):
    if os.environ.has_key(key):
        return os.environ[key]
    else:
        return ""

def show_env(env, full=0):
    env_keys = env.keys()
    env_keys.sort()

    env_filter=["PATH_INFO", "QUERY_STRING", "REQUEST_METHOD", "SCRIPT_NAME"]
    print "Here is the environment:"
    print "<ul>"
    for key in env_keys:
        if full or key in env_filter:
            print "<li>%s=%s" % (key, env[key])
    print "</ul>"

def get_timestamp():
    t = time.time()
    tfrac = int((t - int(t))*100)
    timestamp = time.strftime("%Y-%m-%d_%H:%M:%S.") + "%02d" % tfrac
    return timestamp

def save_file(req, file_field, upload_dir):
    # some debugging...
    F = "FAIL"
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
    return "OK", msg, filepath

def send_response(result, data):
    sys.stdout.write("Content-type: text/html\n\n%s\n" % result)
    sys.stdout.write(data)
    sys.stdout.flush()
    sys.exit(0)

def do_put_object(req, obj_type):
    data_dir = req.config.data_dir + os.sep + obj_type + "s"
    result = "OK"
    msg = ""

    # convert form (cgi.fieldStorage) to dictionary
    try:
        obj_name = req.form["name"].value
    except:
        result = "FAIL"
        msg += "Error: missing %s name in form data" % obj_type
        send_response(result, msg)

    obj_dict = {}
    for k in req.form.keys():
        obj_dict[k] = req.form[k].value

    # sanity check the submitted data
    for field in required_put_fields[obj_type]:
        try:
            value = obj_dict[field]
        except:
            result = "FAIL"
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
        #        result = "FAIL"
        #        msg += "Error: No matching board '%s' registered with server (from field '%s')" % (value, field)
        #        break

    if result != "OK":
        send_response(result, msg)

    if obj_type == "request":
        obj_dict["state"] = "pending"
        timestamp = get_timestamp()
        obj_name += "-" + timestamp

    filename = obj_type + "-" + obj_name
    jfilepath = data_dir + os.sep + filename + ".json"

    # convert to json and save to file
    import json
    data = json.dumps(obj_dict, sort_keys=True, indent=4,
            separators=(',', ': '))
    fout = open(jfilepath, "w")
    fout.write(data+'\n')
    fout.close()

    msg += "%s accepted (filename=%s)\n" % (obj_name, filename)
    send_response(result, msg)

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
        send_response("FAIL", msg)

    filename = obj_type + "-" + obj_name + ".json"
    filepath = data_dir + os.sep + filename
    if not os.path.exists(filepath):
        msg += "Error: filepath %s does not exist" % filepath
        send_response("FAIL", msg)

    # read requested object file
    import json
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
            send_response("FAIL", msg)

    # put dictionary back in json format (beautified)
    data = json.dumps(obj_dict, sort_keys=True, indent=4,
            separators=(',', ': '))
    fout = open(filepath, "w")
    fout.write(data+'\n')
    fout.close()

    send_response("OK", data)

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
        send_response("FAIL", msg)

    if obj_type not in ["board", "resource", "request"]:
        msg = "Error: unsupported object type '%s' for query" % obj_type
        send_response("FAIL", msg)

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

    send_response("OK", msg)


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
        import json

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

    send_response("OK", msg)

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
        send_response("FAIL", msg)

    filename = request_id + ".json"
    filepath = req_data_dir + os.sep + filename
    if not os.path.exists(filepath):
        msg += "Error: filepath %s does not exist" % filepath
        send_response("FAIL", msg)

    # read requested file
    import json
    request_fd = open(filepath, "r")
    mydict = json.load(request_fd)

    # beautify the data, for now
    data = json.dumps(mydict, sort_keys=True, indent=4, separators=(',', ': '))
    send_response("OK", data)

# return the url to download a run package
def do_get_run_url(req):
    run_file_dir = req.config.files_dir + os.sep + "runs"
    msg = ""

    try:
        run_id = req.form["run_id"].value
    except:
        msg += "Error: can't read run_id from form"
        send_response("FAIL", msg)

    filename = run_id + ".frp"
    filepath = run_file_dir + os.sep + filename
    if not os.path.exists(filepath):
        msg += "Error: filepath %s does not exist" % filepath
        send_response("FAIL", msg)

    run_file_url = config.files_url_base + "/files/runs/" + filename
    msg += run_file_url
    send_response("OK", msg)

def do_remove_object(req, obj_type):
    data_dir = req.config.data_dir + os.sep + obj_type + "s"
    msg = ""

    try:
        obj_name = req.form[obj_type].value
    except:
        msg += "Error: can't read '%s' from form" % obj_type
        send_response("FAIL", msg)

    filename = obj_name + ".json"
    filepath = data_dir + os.sep + filename
    if not os.path.exists(filepath):
        msg += "Error: filepath %s does not exist" % filepath
        send_response("FAIL", msg)

    # FIXTHIS - should check permissions here
    # only original-submitter and resource-host are allowed to remove
    os.remove(filepath)

    msg += "%s %s was removed" % (obj_type, obj_name)
    send_response("OK", msg)

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
    import json
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
    print(html)

def show_run_table(req):
    src_dir = req.config.data_dir + os.sep + "runs"

    full_dirlist = os.listdir(src_dir)
    full_dirlist.sort()

    # filter list to only run....json files
    filelist = []
    for f in full_dirlist:
        if f.startswith("run-") and f.endswith(".json"):
            filelist.append(f)

    if not filelist:
        return req.html_error("No request files found.")

    data_url = config.files_url_base + "/data/runs/"
    files_url = config.files_url_base + "/files/runs/"
    html = """<table border="1" cellpadding="2">
  <tr>
    <th>Run Id</th>
    <th>Test</th>
    <th>Spec</th>
    <th>Host</th>
    <th>Board</th>
    <th>Result</th>
    <th>Run File bundle</th>
  </tr>
"""
    import json
    for item in filelist:
        # run_id is the filename with "run-" and ".json" removed
        run_id = item[4:-5]
        run_fd = open(src_dir+os.sep + item, "r")
        run_dict = json.load(run_fd)
        run_fd.close()

        html += '  <tr>\n'
        html += '    <td><a href="'+files_url+run_id+'">'+run_id+'</a></td>\n'
        html += '    <td>%s</td>\n' % run_dict["name"]
        html += '    <td>%s</td>\n' % run_dict["metadata"]["test_spec"]
        html += '    <td>%s</td>\n' % run_dict["metadata"]["host_name"]
        html += '    <td>%s</td>\n' % run_dict["metadata"]["board"]
        html += '    <td><a href="'+data_url+item+'">' + run_dict["status"] + '</a></td>\n'
        filename = item[:-4]+"frp"
        html += '    <td><a href="'+files_url+filename+'">' + filename + '</a></td>\n'
        html += '  </tr>\n'
    html += "</table>"
    print(html)


def do_show(req):
    req.show_header("Lab Control objects")
    #log_this("in do_show, req.page_name='%s'\n" % req.page_name)
    #print("req.page_name='%s' <br><br>" % req.page_name)

    if req.page_name not in ["boards", "resources", "requests", "logs"]:
        title = "Error - unknown object type '%s'" % req.page_name
        req.add_to_message(title)
    else:
        if req.page_name=="boards":
            print("<H1>List of boards</h1>")
            print(file_list_html(req, "data", "boards", ".json"))
        elif req.page_name == "resources":
            print("<H1>List of resources</h1>")
            print(file_list_html(req, "data", "resources", ".json"))
        elif req.page_name == "requests":
            print("<H1>Table of requests</H1>")
            show_request_table(req)
        elif req.page_name == "logs":
            print("<H1>Table of logs</H1>")
            print(file_list_html(req, "files", "logs", ".txt"))

    if req.page_name != "main":
        print("<br><hr>")

    print("<H1>Fuego objects on this server</h1>")
    print("""
Here are links to the different Fuego objects:<br>
<ul>
<li><a href="%(url_base)s/boards">Boards</a></li>
<li><a href="%(url_base)s/resources">Resources</a></li>
<li><a href="%(url_base)s/requests">Requests</a></li>
<li><a href="%(url_base)s/logs">Logs</a></li>
</ul>
<hr>
""" % req.config )

    print("""<a href="%(url_base)s">Back to home page</a>""" % req.config)

def main(req):
    # parse request
    query_string = get_env("QUERY_STRING")

    # determine action, if any
    query_parts = query_string.split("&")
    action = "show"
    for qpart in query_parts:
        if qpart.split("=")[0]=="action":
            action=qpart.split("=")[1]

    #req.add_to_message('action="%s"<br>' % action)

    # get page name
    page_name = get_env("PATH_INFO")
    if not page_name:
        page_name = "/main"
    page_name = os.path.basename(page_name)
    req.set_page_name(page_name)

    #req.add_to_message("page_name=%s" % page_name)

    req.action = action

    # NOTE: uncomment this when you get a 500 error
    #req.show_header('Debug')
    #show_env(os.environ)
    log_this("in main request loop: action='%s'<br>" % action)
    #print("in main request loop: action='%s'<br>" % action)

    # perform action
    req.form = cgi.FieldStorage()

    action_list = ["show", "add_board", "add_resource", "add_request",
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
            send_response("FAIL", msg)

        action_function(req)

        # NOTE: computer actions don't return to here, but 'show' does
        return

    req.show_header("LabControl server Error")
    print(req.html_error("Unknown action '%s'" % action))


req = req_class(config)

if __name__=="__main__":
    try:
        main(req)
        req.show_message()
    except SystemExit:
        pass
    except:
        req.show_header("LabControl Server Error")
        print """<font color="red">Exception raised by lcserver software</font>"""
        # show traceback information here:
        print "<pre>"
        import traceback
        (etype, evalue, etb) = sys.exc_info()
        traceback.print_exception(etype, evalue, etb, None, sys.stdout)
        print "</pre>"

