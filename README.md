# ezpbars websocket backend

websocket backend for ezpbars using [python](https://www.python.org/) + [FastAPI](https://fastapi.tiangolo.com/)

## Getting Started

```sh
python -m venv venv
. venv/bin/activate
python -m pip install -U pip
pip install -r requirements.txt
uvicorn main:app --reload
```

## Contributing

This project uses [black](https://github.com/psf/black) for linting
and [unittest](https://docs.python.org/3/library/unittest.html) for testing.
All tests must pass, but there's no specific code coverage requirement.
