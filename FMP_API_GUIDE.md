# Financial Modeling Prep API Integration Guide

## Overview

The RS Analysis Dashboard now integrates with **Financial Modeling Prep (FMP)** API to provide enriched company fundamentals and financial data for any ticker in your dataset.

## Getting Started

### 1. Get Your API Key

1. Visit: https://financialmodelingprep.com/
2. Sign up for a **free account**
3. Copy your API key from the dashboard
4. Enter it in the Streamlit app sidebar under "🔑 API Configuration"

### 2. Using the API Features

Once you've entered your API key, a new **"💼 Company Details"** tab becomes available with the following information:

## Available Features

### 📊 Company Profile
- **Company Name** - Full legal company name
- **Sector** - Industry sector (Technology, Healthcare, etc.)
- **Industry** - Specific industry classification
- **Market Cap** - Current market capitalization
- **Stock Price** - Current stock price
- **Exchange** - Trading exchange (NASDAQ, NYSE, etc.)
- **Website** - Company website URL
- **Description** - Company business description

### 📈 Key Metrics
- **PE Ratio** - Price-to-Earnings ratio
- **ROE** - Return on Equity percentage
- **Debt-to-Equity** - Leverage ratio
- **Current Ratio** - Short-term liquidity measure
- **Quick Ratio** - Immediate liquidity measure
- **PB Ratio** - Price-to-Book ratio

### 📊 Financial Ratios
- **Gross Profit Margin** - Gross profitability percentage
- **Operating Profit Margin** - Operating efficiency percentage
- **Net Profit Margin** - Bottom-line profitability percentage
- **Asset Turnover** - How efficiently assets generate revenue
- **Debt-to-Assets** - Financial leverage indicator

### 📅 Earnings Information
- **Upcoming Earnings Dates** - Next earnings announcement dates
- **EPS Estimates** - Analyst consensus EPS predictions
- **Actual EPS** - Historical earnings per share results

## How to Use

1. **Open the Streamlit app** and navigate to the "💼 Company Details" tab
2. **Paste your API key** in the sidebar under "API Configuration"
3. **Select a ticker** from the dropdown menu
4. **View enriched company data** including:
   - Company profile and background
   - Key financial metrics
   - Profitability and efficiency ratios
   - Recent earnings dates and results

## API Limits

**Free Plan:**
- 250 API calls per month
- Limited to historical data only
- Perfect for small analysis sessions

**Paid Plans:**
- Higher call limits
- More endpoints available
- Real-time data access

## Workflow Example

1. Filter your RS data by sector, percentile, or date range
2. Navigate to "💼 Company Details" tab
3. Select a top performer from your filtered results
4. Instantly view fundamental metrics and ratios
5. Compare against peers by selecting different tickers

## Tips & Best Practices

- **Cache Results**: The app caches API results to minimize API calls
- **Batch Analysis**: Analyze multiple tickers without hitting API limits (cached)
- **Compare Metrics**: Use metrics across different tickers to identify outliers
- **Combine Data**: Use RS rankings with fundamental metrics for deeper analysis

## API Endpoints Used

The integration uses these FMP API endpoints:
- `/api/v3/profile/{ticker}` - Company profile data
- `/api/v3/key-metrics/{ticker}` - Key financial metrics
- `/api/v3/ratios/{ticker}` - Financial ratios
- `/api/v4/earning_calendar` - Earnings dates

## Error Handling

If you encounter errors:
- ❌ "API Key Invalid" - Check your API key format
- ❌ "Ticker Not Found" - Verify ticker symbol exists
- ❌ "Rate Limit Exceeded" - Wait a moment, refresh your cache, or upgrade your plan
- ❌ "No Data Available" - Some tickers may not have data in FMP database

## Privacy & Security

- API keys are **never stored** - entered fresh each session
- All API calls are made directly to FMP servers
- Data is cached locally in your Streamlit session
- No data is sent anywhere except to FMP servers

## API Pricing

Visit https://financialmodelingprep.com/ for current pricing:
- **Free**: 250 calls/month (perfect for analysis)
- **Starter**: Higher limits and priority support
- **Professional**: Enterprise features and real-time data

## Support

- **API Documentation**: https://site.financialmodelingprep.com/developer/docs
- **FMP Support**: Contact FMP support through their website
- **App Issues**: Check app syntax with `python3 -m py_compile app.py`

---

**Enhanced Dashboard Features:**
- RS calculations from 1000+ trading days
- Historical trends and analysis
- Fundamental metrics from FMP
- Compare relative strength with financial health
