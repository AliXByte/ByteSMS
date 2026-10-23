@echo off
title ByteSMS Panel
color 0A
echo ===================================================
echo            ByteSMS - Smart Panel
echo ===================================================
echo Installing requirements...
pip install -r requirements.txt
echo Starting server...
python app.py
pause