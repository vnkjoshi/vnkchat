```markdown
# Contributing

Thanks for helping improve the Swing Algo Trading app!  

## Setup

1. **Clone & venv**  
   ```bash
   git clone <repo-url>
   cd swing-algo
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt

2. **Database**  
cp .env.example .env       # fill in your local creds
flask db upgrade

3. **Run**  
flask run
celery -A celery_app:celery worker -P eventlet
celery -A celery_app:celery beat


Testing

Unit tests live under tests/
Run them with
pytest -q

Code Style
Follow PEP8.
We use black + isort:
pip install black isort
black .
isort .


Adding Features
Feature flags in config.py → FEATURE_FLAGS.
New Celery tasks → register in tasks.py + add to celery_app.py’s beat_schedule.
Instrument new metrics → use Prometheus client as shown in main.py / celery_app.py.

License
MIT © Your Name
