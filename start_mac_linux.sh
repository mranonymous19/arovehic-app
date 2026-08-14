#!/bin/bash
cd "$(dirname "$0")"
echo "Installing requirements (only needed the first time)..."
pip3 install -r requirements.txt
echo ""
if [ ! -f .env ]; then
    echo "No .env file found."
    echo "Create one with DATABASE_URL and SECRET_KEY before running (see README)."
    exit 1
fi
echo "Starting Shopify Purchase Tracker..."
python3 app.py
