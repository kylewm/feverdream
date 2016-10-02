from flask_sqlalchemy import SQLAlchemy
from flask_wtf import CsrfProtect
from flask_session import Session
from redis import StrictRedis


db = SQLAlchemy()
csrf = CsrfProtect()
sess = Session()
redis = StrictRedis()


def init_app(app):
    db.init_app(app)
    csrf.init_app(app)
    sess.init_app(app)
