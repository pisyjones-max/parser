# Zoon parser

Парсер организаций Zoon.ru для наполнения собственного каталога.

## Локально

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium
python parser/zoon_parser.py --max-pages 1 --max-companies 30
```

## GitHub Actions

Actions → **Zoon parser** → **Run workflow**.

Первый запуск: `max_pages=1`, `max_companies=30`.

После проверки: `max_pages=0`, `max_companies=0`.

Результат: `data/companies.json`.

Парсер не обходит CAPTCHA, авторизацию или другие ограничения доступа.
