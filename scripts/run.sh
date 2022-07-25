#!/usr/bin/env bash
main() {
    . venv/bin/activate
    uvicorn main:app --port 80
}

main
