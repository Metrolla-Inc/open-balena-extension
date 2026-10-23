"""gunicorn -w 1 --threads 8 --bind 0.0.0.0:80 wsgi:app"""
import logging
import os

from fleet_api.app import create_app

logging.basicConfig(level=os.environ.get('LOG_LEVEL', 'INFO'),
                    format='%(asctime)s %(levelname)s %(name)s %(message)s')
app = create_app()
