#!/bin/bash
# Quick Start Script for RS Dashboard

echo "=========================================="
echo "  RS Analysis Dashboard - Quick Start"
echo "=========================================="
echo ""

# Check if historical data exists
if [ ! -f "output/rs_historical_all.csv" ]; then
    echo "📊 Building historical data from git commits..."
    echo "   This extracts 1,268 days of data (~5.8M records)"
    echo "   Please wait, this may take 2-3 minutes..."
    echo ""
    python3 build_historical_data.py
    echo ""
else
    echo "✅ Historical data already exists!"
    echo "   File: output/rs_historical_all.csv"
    echo "   Size: $(du -h output/rs_historical_all.csv | cut -f1)"
    echo ""
fi

echo "🚀 Starting Streamlit app..."
echo "   Open your browser to: http://localhost:8501"
echo ""

# Install dependencies if needed
if ! pip3 show streamlit > /dev/null 2>&1; then
    echo "📦 Installing required packages..."
    pip3 install -r requirements.txt
fi

# Start the app
streamlit run app.py
