# encoding: utf-8
#
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.
#
# Author: Kyle Lahnakoski (kyle@lahnakoski.com)
#
from __future__ import absolute_import
from __future__ import division
from __future__ import unicode_literals

import os
import sys
from _ssl import PROTOCOL_SSLv23
from collections import Mapping
from ssl import SSLContext
from tempfile import NamedTemporaryFile

import flask
from flask import Flask
from werkzeug.contrib.fixers import HeaderRewriterFix
from werkzeug.wrappers import Response

import active_data
from active_data import record_request, cors_wrapper
from active_data.actions import save_query
from active_data.actions.json import get_raw_json
from active_data.actions.jx import jx_query
from active_data.actions.save_query import SaveQueries, find_query
from active_data.actions.sql import sql_query
from active_data.actions.static import download
from jx_base import container
from mo_files import File
from mo_logs import Log
from mo_logs import constants, startup
from mo_threads import Thread
from pyLibrary import convert
from pyLibrary.env import elasticsearch

OVERVIEW = File("active_data/public/index.html").read()

flask_app = Flask(__name__)
config = None


@flask_app.route('/', defaults={'path': ''}, methods=['OPTIONS', 'HEAD'])
@flask_app.route('/<path:path>', methods=['OPTIONS', 'HEAD'])
@cors_wrapper
def _head(path):
    return Response(b'', status=200)

flask_app.add_url_rule('/tools/<path:filename>', None, download)
flask_app.add_url_rule('/find/<path:hash>', None, find_query)
flask_app.add_url_rule('/query', None, jx_query, defaults={'path': ''}, methods=['GET', 'POST'])
flask_app.add_url_rule('/query/', None, jx_query, defaults={'path': ''}, methods=['GET', 'POST'])
flask_app.add_url_rule('/sql', None, sql_query, defaults={'path': ''}, methods=['GET', 'POST'])
flask_app.add_url_rule('/sql/', None, sql_query, defaults={'path': ''}, methods=['GET', 'POST'])
flask_app.add_url_rule('/query/<path:path>', None, jx_query, defaults={'path': ''}, methods=['GET', 'POST'])
flask_app.add_url_rule('/json/<path:path>', None, get_raw_json, methods=['GET'])


@flask_app.route('/', defaults={'path': ''}, methods=['GET', 'POST'])
@cors_wrapper
def _default(path):
    record_request(flask.request, None, flask.request.get_data(), None)

    return Response(
        convert.unicode2utf8(OVERVIEW),
        status=200,
        headers={
            "Content-Type": "text/html"
        }
    )


def setup():
    global config

    config = startup.read_settings(
        defs=[
            {
                "name": ["--process_num", "--process"],
                "help": "Additional port offset (for multiple Flask processes",
                "type": int,
                "dest": "process_num",
                "default": 0,
                "required": False
            },
            {
                "name": "app_name",
                "help": "gunicorn supplied argument",
                "type": str
            }
        ],
        env_filename=os.environ.get('ACTIVEDATA_CONFIG')
    )

    constants.set(config.constants)
    Log.start(config.debug)

    # PIPE REQUEST LOGS TO ES DEBUG
    if config.request_logs:
        request_logger = elasticsearch.Cluster(config.request_logs).get_or_create_index(config.request_logs)
        active_data.request_log_queue = request_logger.threaded_queue(max_size=2000)

    # SETUP DEFAULT CONTAINER, SO THERE IS SOMETHING TO QUERY
    container.config.default = {
        "type": "elasticsearch",
        "settings": config.elasticsearch.copy()
    }

    # TRIGGER FIRST INSTANCE
    if config.saved_queries:
        setattr(save_query, "query_finder", SaveQueries(config.saved_queries))

    HeaderRewriterFix(flask_app, remove_headers=['Date', 'Server'])


def run_flask():
    if config.flask.port and config.args.process_num:
        config.flask.port += config.args.process_num

    # TURN ON /exit FOR WINDOWS DEBUGGING
    if config.flask.debug or config.flask.allow_exit:
        config.flask.allow_exit = None
        Log.warning("ActiveData is in debug mode")
        flask_app.add_url_rule('/exit', 'exit', _exit)

    if config.flask.ssl_context:
        if config.args.process_num:
            Log.error("can not serve ssl and multiple Flask instances at once")
        setup_flask_ssl()

    flask_app.run(**config.flask)



gunicorn_app = None


def setup_gunicorn():
    global gunicorn_app
    from gunicorn.app.base import BaseApplication

    print("make class")
    class GunicornApp(BaseApplication):

        def load(self):
            print("return app")

            return flask_app

        def load_config(self):
            pass

        def run(self):
            try:
                BaseApplication.run(self)
            except BaseException as e:  # MUST CATCH BaseException BECAUSE argparse LIKES TO EXIT THAT WAY, AND gunicorn WILL NOT REPORT
                Log.warning("Serious problem with ActiveData service construction!  Shutdown!", cause=e)
            finally:
                Log.stop()

    gunicorn_app = GunicornApp()


def setup_flask_ssl():
    config.flask.ssl_context = None

    if not config.flask.ssl_context:
        return

    ssl_flask = config.flask.copy()
    ssl_flask.debug = False
    ssl_flask.port = 443

    if isinstance(config.flask.ssl_context, Mapping):
        # EXPECTED PEM ENCODED FILE NAMES
        # `load_cert_chain` REQUIRES CONCATENATED LIST OF CERTS
        tempfile = NamedTemporaryFile(delete=False, suffix=".pem")
        try:
            tempfile.write(File(ssl_flask.ssl_context.certificate_file).read_bytes())
            if ssl_flask.ssl_context.certificate_chain_file:
                tempfile.write(File(ssl_flask.ssl_context.certificate_chain_file).read_bytes())
            tempfile.flush()
            tempfile.close()

            context = SSLContext(PROTOCOL_SSLv23)
            context.load_cert_chain(tempfile.name, keyfile=File(ssl_flask.ssl_context.privatekey_file).abspath)

            ssl_flask.ssl_context = context
        except Exception, e:
            Log.error("Could not handle ssl context construction", cause=e)
        finally:
            try:
                tempfile.delete()
            except Exception:
                pass

    def runner(please_stop):
        Log.warning("ActiveData listening on encrypted port {{port}}", port=ssl_flask.port)
        flask_app.run(**ssl_flask)

    Thread.run("SSL Server", runner)

    if config.flask.ssl_context and config.flask.port != 80:
        Log.warning("ActiveData has SSL context, but is still listening on non-encrypted http port {{port}}", port=config.flask.port)

    config.flask.ssl_context = None


def _exit():
    Log.note("Got request to shutdown")
    shutdown = flask.request.environ.get('werkzeug.server.shutdown')
    if shutdown:
        shutdown()
    else:
        Log.warning("werkzeug.server.shutdown does not exist")

    return Response(
        convert.unicode2utf8(OVERVIEW),
        status=400,
        headers={
            "Content-Type": "text/html"
        }
    )


if __name__ in ("__main__", "active_data.app"):
    try:
        setup()
    except BaseException as e:  # MUST CATCH BaseException BECAUSE argparse LIKES TO EXIT THAT WAY, AND gunicorn WILL NOT REPORT
        try:
            Log.error("Serious problem with ActiveData service construction!  Shutdown!", cause=e)
        finally:
            Log.stop()

    if config.flask:
        try:
            run_flask()
        except BaseException as e:  # MUST CATCH BaseException BECAUSE argparse LIKES TO EXIT THAT WAY, AND gunicorn WILL NOT REPORT
            Log.warning("Serious problem with ActiveData service construction!  Shutdown!", cause=e)
        finally:
            Log.stop()
    else:
        setup_gunicorn()
