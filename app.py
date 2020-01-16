#!/usr/bin/python3
from flask import Flask, render_template, make_response, request, jsonify, \
    redirect, send_from_directory
from flask_caching import Cache
import json
import os
import tempfile
from concurrent.futures import ThreadPoolExecutor
import requests
import icalendar
import datetime
from dateutil.rrule import rrulestr
from pprint import pprint
import yaml
import recurring_ical_events
import traceback
import io
import sys

# configuration
DEBUG = os.environ.get("APP_DEBUG", "true").lower() == "true"
PORT = int(os.environ.get("PORT", "5000"))
CACHE_REQUESTED_URLS_FOR_SECONDS = int(os.environ.get("CACHE_REQUESTED_URLS_FOR_SECONDS", 600))

# TODO: add as parameters
MAXIMUM_THREADS = 100

# constants
HERE = os.path.dirname(__name__) or "."
DEFAULT_SPECIFICATION_PATH = os.path.join(HERE, "default_specification.json")
TEMPLATE_FOLDER_NAME = "templates"
TEMPLATE_FOLDER = os.path.join(HERE, TEMPLATE_FOLDER_NAME)
CALENDARS_TEMPLATE_FOLDER_NAME = "calendars"
CALENDAR_TEMPLATE_FOLDER = os.path.join(TEMPLATE_FOLDER, CALENDARS_TEMPLATE_FOLDER_NAME)
STATIC_FOLDER_NAME = "static"
STATIC_FOLDER_PATH = os.path.join(HERE, STATIC_FOLDER_NAME)
DHTMLX_LANGUAGES_FILE = os.path.join(STATIC_FOLDER_PATH, "js", "dhtmlx", "locale", "languages.json")

# specification
PARAM_SPECIFICATION_URL = "specification_url"

# globals
app = Flask(__name__, template_folder="templates")
# Check Configuring Flask-Cache section for more details
cache = Cache(app, config={
    'CACHE_TYPE': 'filesystem',
    'CACHE_DIR': tempfile.mktemp(prefix="cache-")})

# caching

__URL_CACHE = {}
def cache_url(url, text):
    """Cache the value of a url."""
    __URL_CACHE[url] = text
    try:
        get_text_from_url(url)
    finally:
        del __URL_CACHE[url]

# configuration

def get_configuration():
    """Return the configuration for the browser"""
    config = {
        "default_specification": get_default_specification()
    }
    with open(DHTMLX_LANGUAGES_FILE, encoding="UTF-8") as file:
        config["dhtmlx"] = {
            "languages" : json.load(file)
        }
    return config

def set_JS_headers(response):
    repsonse = make_response(response)
    response.headers['Access-Control-Allow-Origin'] = '*'
    # see https://developer.mozilla.org/en-US/docs/Web/HTTP/CORS/Errors/CORSMissingAllowHeaderFromPreflight
    response.headers['Access-Control-Allow-Headers'] = request.headers.get("Access-Control-Request-Headers")
    response.headers['Content-Type'] = 'text/calendar'
    return response

def set_js_headers(func):
    """Set the response headers for a valid CORS request."""
    def with_js_response(*args, **kw):
        return set_JS_headers(func(*args, **kw))
    return with_js_response

@cache.memoize(
    CACHE_REQUESTED_URLS_FOR_SECONDS,
    forced_update=lambda: bool(__URL_CACHE))
def get_text_from_url(url):
    """Return the text from a url.

    The result is cached CACHE_REQUESTED_URLS_FOR_SECONDS.
    """
    if __URL_CACHE:
        return __URL_CACHE[url]
    return requests.get(url).text

def get_default_specification():
    """Return the default specification."""
    with open(DEFAULT_SPECIFICATION_PATH, encoding="UTF-8") as file:
        return json.load(file)

def get_specification(query=None):
    """Build the calendar specification."""
    if query is None:
        query = request.args
    specification = get_default_specification()
    # get a request parameter, see https://stackoverflow.com/a/11774434
    url = query.get(PARAM_SPECIFICATION_URL, None)
    if url:
        url_specification_response = get_text_from_url(url)
        try:
            url_specification_values = json.loads(url_specification_response)
        except json.JSONDecodeError:
            url_specification_values = yaml.safe_load(url_specification_response)
        specification.update(url_specification_values)
    for parameter in query:
        # get a list of arguments
        # see http://werkzeug.pocoo.org/docs/0.14/datastructures/#werkzeug.datastructures.MultiDict
        value = query.getlist(parameter, None)
        if len(value) == 1:
            value = value[0]
        specification[parameter] = value
    return specification


class ConvertToDhtmlx:
    """Convert events to dhtmlx. This conforms to a stratey pattern."""
    
    def __init__(self, timeshift_minutes):
        """Create a DHTMLX conversion strategy.
        
        - timeshift_minutes is the timeshift specified by the calendar
            for dates.
        """
        self.timeshift = timeshift_minutes

    def date_to_string(self, date):
        """Convert a date to a string.


        """
        # use ISO format
        # see https://docs.dhtmlx.com/scheduler/howtostart_nodejs.html#step4implementingcrud
        # see https://docs.python.org/3/library/datetime.html#datetime.datetime.isoformat
        # see https://docs.python.org/3/library/datetime.html#strftime-and-strptime-behavior
        timezone = datetime.timezone(datetime.timedelta(minutes=-self.timeshift))
        if isinstance(date, datetime.date) and not isinstance(date, datetime.datetime):
            date = datetime.datetime(date.year, date.month, date.day, tzinfo=timezone)
        elif date.tzinfo is None:
            date = date.replace(tzinfo=timezone)
        date = date.astimezone(datetime.timezone.utc)
        return date.strftime("%Y-%m-%d %H:%M")


    def convert_ical_event(self, calendar_event):
        start = calendar_event["DTSTART"].dt
        end = calendar_event.get("DTEND", calendar_event["DTSTART"]).dt
        geo = calendar_event.get("GEO", None)
        if geo:
            geo = {"lon": geo.longitude, "lat": geo.latitude}
        name = calendar_event.get("SUMMARY", "")
        sequence = str(calendar_event.get("SEQUENCE", 0))
        uid = calendar_event["UID"]
        start_date = self.date_to_string(start)
        return {
            "start_date": start_date,
            "end_date": self.date_to_string(end),
            "start_date_iso": start.isoformat(),
            "end_date_iso": end.isoformat(),
            "start_date_iso_0": start.isoformat(),
            "end_date_iso_0": end.isoformat(),
            "text":  name,
            "description": calendar_event.get("DESCRIPTION", ""),
            "location": calendar_event.get("LOCATION", None),
            "geo": geo,
            "uid": uid,
            "ical": calendar_event.to_ical().decode("UTF-8"),
            "sequence": sequence,
            "recurrence": None,
            "url": calendar_event.get("URL"),
            "id": (uid, start_date),
            "type": "event"
        }

    def error(self, ty, error, tb, url=None):
        """Create an error which can be used by the dhtmlx scheduler."""
        now = datetime.datetime.now();
        now_iso = now.isoformat()
        now_s = self.date_to_string(now)
        tb_s = io.StringIO()
        traceback.print_exception(ty, error, tb, file=tb_s)
        return {
            "start_date": now_s,
            "end_date": now_s,
            "start_date_iso": now_iso,
            "end_date_iso": now_iso,
            "start_date_iso_0": now_iso,
            "end_date_iso_0": now_iso,
            "text":  type(error).__name__,
            "description": str(error),
            "traceback": tb_s.getvalue(),
            "location": None,
            "geo": None,
            "uid": "error",
            "ical": "",
            "sequence": 0,
            "recurrence": None,
            "url": url,
            "id": id(error),
            "type": "error"
        }

def retrieve_calendar(url, specification, conversion_strategy):
    """Get the calendar entry from a url.

    Also unfold the events to past and future.
    see https://dateutil.readthedocs.io/en/stable/rrule.html
    """
    try:
        calendar_text = get_text_from_url(url)
        calendars = icalendar.Calendar.from_ical(calendar_text, multiple=True)
        # collect latest event information
        ical_events = []
        today = datetime.datetime.utcnow()
        one_year_ahead = today.replace(year=today.year + 1)
        one_year_before = today.replace(year=today.year - 1)
        for calendar in calendars:
            ical_events.extend(recurring_ical_events.of(calendar).between(one_year_before, one_year_ahead))
        # collect events and their recurrences  
        events = []
        for calendar_event in ical_events:
            event = conversion_strategy.convert_ical_event(calendar_event)
            events.append(event)
        return events
    except:
        ty, err, tb = sys.exc_info()
        error = conversion_strategy.error(ty, err, tb, url=url)
        return [error]

def get_events(specification, conversion_strategy):
    """Return events."""
    urls = specification["url"]
    if isinstance(urls, str):
        urls = [urls]
    assert len(urls) <= MAXIMUM_THREADS, "You can only merge {} urls.".format(MAXIMUM_THREADS)
    all_events = []
    with ThreadPoolExecutor(max_workers=MAXIMUM_THREADS) as e:
        events_list = e.map(lambda url: retrieve_calendar(url, specification, conversion_strategy), urls)
        for events in events_list:
            all_events.extend(events)
    return all_events

def render_app_template(template, specification):
    return render_template(template,
        specification=specification,
        get_events=get_events,
        json=json,
    )

@app.route("/calendar.<type>", methods=['GET', 'OPTIONS'])
# use query string in cache, see https://stackoverflow.com/a/47181782/1320237
#@cache.cached(timeout=CACHE_TIMEOUT, query_string=True)
def get_calendar(type):
    """Return a calendar."""
    specification = get_specification()
    if type == "spec":
        return jsonify(specification)
    if type == "events.json":
        entries = get_events(specification, ConvertToDhtmlx(int(specification["timeshift"])))
        return jsonify(entries)
    if type == "html":
        template_name = specification["template"]
        all_template_names = os.listdir(CALENDAR_TEMPLATE_FOLDER)
        assert template_name in all_template_names, "Template names must be file names like \"{}\", not \"{}\".".format("\", \"".join(all_template_names), template_name)
        template = os.path.join(CALENDARS_TEMPLATE_FOLDER_NAME, template_name)
        return render_app_template(template, specification)
    raise ValueError("Cannot use extension {}. Please see the documentation or report an error.".format(type))

for folder_name in os.listdir(STATIC_FOLDER_PATH):
    folder_path = os.path.join(STATIC_FOLDER_PATH, folder_name)
    if not os.path.isdir(folder_path):
        continue
    @app.route('/' + folder_name + '/<path:path>', endpoint="static/" + folder_name)
    def send_static(path, folder_name=folder_name):
        return send_from_directory('static/' + folder_name, path)

@app.route("/")
def serve_index():
    return send_from_directory("static", "index.html")

@app.route("/configuration.js")
def serve_configuration():
    return "/* generated */\nconst configuration = {};".format(json.dumps(get_configuration()))

@app.errorhandler(500)
def unhandledException(error):
    """Called when an error occurs.

    See https://stackoverflow.com/q/14993318
    """
    file = io.StringIO()
    traceback.print_exception(type(error), error, error.__traceback__, file=file)
    return """
    <!DOCTYPE HTML PUBLIC "-//W3C//DTD HTML 3.2 Final//EN">
    <html>
        <head>
            <title>500 Internal Server Error</title>
        </head>
        <body>
            <h1>Internal Server Error</h1>
            <p>The server encountered an internal error and was unable to complete your request.  Either the server is overloaded or there is an error in the application.</p>
            <pre>\r\n{traceback}
            </pre>
        </body>
    </html>
    """.format(traceback=file.getvalue()), 500 # return error code from https://stackoverflow.com/a/7824605

if __name__ == "__main__":
    app.run(debug=DEBUG, host="0.0.0.0", port=PORT)
