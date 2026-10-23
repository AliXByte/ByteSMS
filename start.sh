#!/bin/bash
echo "Installing requirements..."
pip3 install -r requirements.txt
echo "Starting server..."
python3 app.py