#!/bin/bash

sudo find . -type d -name "__pycache__" -exec rm -rf {} +
sudo find . -type d -name ".ipynb_checkpoints" -exec rm -rf {} +
sudo find . -type d -name ".Trash-0" -exec rm -rf {} +
