from flask import Flask
from silopub.views import views
from silopub import wordpress
from silopub import blogger
from silopub import tumblr
from silopub import twitter
from silopub import facebook
from silopub import flickr
from silopub import github
from silopub import goodreads
from silopub import micropub
from silopub import ext
from silopub.models import *  # NOQA
import logging
import logging.handlers
import sys

MAIL_FORMAT = '''\
Message type:       %(levelname)s
Location:           %(pathname)s:%(lineno)d
Module:             %(module)s
Function:           %(funcName)s
Time:               %(asctime)s

Message:

%(message)s
'''


def create_app(config_path='../silopub.cfg', configurator=None):
    app = Flask(__name__)
    if configurator:
        configurator(app)
    else:
        app.config.from_pyfile(config_path)

    configure_logging(app)
    ext.init_app(app)

    app.register_blueprint(views)
    app.register_blueprint(wordpress.wordpress)
    app.register_blueprint(blogger.blogger)
    app.register_blueprint(tumblr.tumblr)
    app.register_blueprint(twitter.twitter)
    app.register_blueprint(facebook.facebook)
    app.register_blueprint(flickr.flickr)
    app.register_blueprint(goodreads.goodreads)
    app.register_blueprint(github.github)
    app.register_blueprint(micropub.micropub)

    micropub.register_service('wordpress', wordpress)
    micropub.register_service('tumblr', tumblr)
    micropub.register_service('blogger', blogger)
    micropub.register_service('twitter', twitter)
    micropub.register_service('facebook', facebook)
    micropub.register_service('flickr', flickr)
    micropub.register_service('github', github)
    micropub.register_service('goodreads', goodreads)

    return app


def configure_logging(app):
    if app.debug:
        return

    app.logger.setLevel(logging.DEBUG)
    app.logger.addHandler(logging.StreamHandler(sys.stdout))

    recipients = app.config.get('ADMIN_EMAILS')
    if recipients:
        error_handler = logging.handlers.SMTPHandler(
            'localhost', 'silo.pub <silopub@kylewm.com>',
            recipients, 'silo.pub error')
        error_handler.setLevel(logging.ERROR)
        error_handler.setFormatter(logging.Formatter(MAIL_FORMAT))
        app.logger.addHandler(error_handler)
