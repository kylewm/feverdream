import sys
import os
import pytest

sys.path.append(os.path.join(os.path.dirname(__file__),  '..'))

import json
import silopub  # NOQA


class TestConfig:
    SECRET_KEY = 'lmnop8765309'
    SESSION_TYPE = 'filesystem'
    DEBUG = True
    SQLALCHEMY_DATABASE_URI = 'sqlite:///:memory:'
    GOOGLE_CLIENT_ID = 'google-client-id'
    GOOGLE_CLIENT_SECRET = 'google-client-secret'
    TUMBLR_CLIENT_KEY = 'tumblr-client-key'
    TUMBLR_CLIENT_SECRET = 'tumblr-client-secret'
    WORDPRESS_CLIENT_ID = 'wordpress-client-id'
    WORDPRESS_CLIENT_SECRET = 'wordpress-client-secret'
    TWITTER_CLIENT_KEY = 'twitter-client-key'
    TWITTER_CLIENT_SECRET = 'twitter-client-secret'
    FACEBOOK_CLIENT_ID = 'facebook-client-id'
    FACEBOOK_CLIENT_SECRET = 'facebook-client-secret'
    FLICKR_CLIENT_KEY = 'flickr-client-key'
    FLICKR_CLIENT_SECRET = 'flickr-client-secret'


@pytest.fixture
def app():
    app = silopub.create_app(
        configurator=lambda app: app.config.from_object(TestConfig))
    db = silopub.ext.db
    with app.app_context():
        db.create_all()
        with app.test_request_context():
            yield app
        db.drop_all()


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def db(app):
    return silopub.ext.db
