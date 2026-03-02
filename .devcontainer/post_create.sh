#!/bin/bash

# Suppress 'detected dubious ownership' errors for git commands.
# Must be run every time the container starts since VSCode copies
# $HOME/.gitconfig of host machine to container on every start.
git config --global --add safe.directory '*'

# Install project dependencies
pip install poetry
poetry install

# Install pre-commit hooks
poetry run pre-commit install
