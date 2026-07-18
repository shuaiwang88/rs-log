#!/usr/bin/env python3
"""
This script fetches stock tickers from Tweevest pattern scanners.
It can discover and fetch all scanner patterns dynamically from
https://tweevest.com/pattern-finder, or fetch a specific pattern.
"""

import re
import json
import sys
import os
import time
import requests

BASE_URL = 'https://tweevest.com'
PATTERN_INDEX_URL = 'https://tweevest.com/pattern-finder'

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7',
    'Accept-Language': 'en-US,en;q=0.9',
    'Sec-Ch-Ua': '"Not/A)Brand";v="8", "Chromium";v="126", "Google Chrome";v="126"',
    'Sec-Ch-Ua-Mobile': '?0',
    'Sec-Ch-Ua-Platform': '"macOS"',
    'Sec-Fetch-Dest': 'document',
    'Sec-Fetch-Mode': 'navigate',
    'Sec-Fetch-Site': 'none',
    'Sec-Fetch-User': '?1',
    'Upgrade-Insecure-Requests': '1'
}

def discover_patterns(retries=2, backoff=2):
    """Fetches the main pattern finder page and extracts scanner links with retries."""
    for attempt in range(1, retries + 2):
        print(f"Discovering scanner patterns from {PATTERN_INDEX_URL} (Attempt {attempt})...", file=sys.stderr)
        try:
            session = requests.Session()
            response = session.get(PATTERN_INDEX_URL, headers=HEADERS, timeout=20)
            if response.status_code == 200:
                content = response.text
                # Find all /pattern-finder/... links
                links = re.findall(r'href=["\'](/pattern-finder/[a-zA-Z0-9_-]+)["\']', content)
                
                # Filter out next.js internal / system paths
                patterns = []
                for path in sorted(list(set(links))):
                    name = path.split('/')[-1]
                    if name in ('pattern-finder', 'layout', 'page', 'error') or any(name.startswith(p) for p in ('layout-', 'page-', 'error-')):
                        continue
                    patterns.append((name, BASE_URL + path))
                    
                return patterns
            
            print(f"Error: Received status code {response.status_code} from index page (Attempt {attempt}).", file=sys.stderr)
        except Exception as e:
            print(f"Attempt {attempt} failed to discover patterns: {e}", file=sys.stderr)
            
        if attempt <= retries:
            time.sleep(backoff * attempt)
            
    print(f"Failed to discover patterns after {retries + 1} attempts.", file=sys.stderr)
    return []

def fetch_tickers_for_url(url, pattern_name, retries=2, backoff=2):
    """Fetches tickers from a specific scanner pattern URL with retry support."""
    for attempt in range(1, retries + 2):
        print(f"Fetching tickers for '{pattern_name}' from {url} (Attempt {attempt})...", file=sys.stderr)
        try:
            session = requests.Session()
            response = session.get(url, headers=HEADERS, timeout=20)
            
            if response.status_code == 200:
                content = response.text
                
                # Search for "results" (possibly escaped) followed by colon and bracket
                pattern = r'results\\?"\s*:\s*\\?\['
                matches = list(re.finditer(pattern, content))
                
                if not matches:
                    print(f"Warning: Could not find results data for '{pattern_name}'.", file=sys.stderr)
                    return []
                    
                start_pos = content.find('[', matches[0].start())
                if start_pos == -1:
                    return []
                    
                bracket_count = 0
                end_pos = start_pos
                in_string = False
                escape = False
                
                for i in range(start_pos, len(content)):
                    char = content[i]
                    if escape:
                        escape = False
                        continue
                    if char == '\\':
                        escape = True
                        continue
                    if char == '"':
                        in_string = not in_string
                        continue
                    if not in_string:
                        if char == '[':
                            bracket_count += 1
                        elif char == ']':
                            bracket_count -= 1
                            if bracket_count == 0:
                                end_pos = i + 1
                                break
                                
                array_str = content[start_pos:end_pos]
                parsed_array = None
                
                # Try decoding JSON in different formats (escaped string vs raw array)
                try:
                    escaped_str = '"' + array_str.replace('"', '\\"').replace('\\\\"', '\\"') + '"'
                    decoded_str = json.loads(escaped_str)
                    parsed_array = json.loads(decoded_str)
                except Exception:
                    pass
                    
                if parsed_array is None:
                    try:
                        parsed_array = json.loads(array_str)
                    except Exception:
                        pass
                        
                if parsed_array is None:
                    try:
                        cleaned = array_str.replace('\\"', '"').replace('\\\\', '\\')
                        parsed_array = json.loads(cleaned)
                    except Exception:
                        pass
                        
                if parsed_array is not None:
                    symbols = [
                        item.get("symbol") 
                        for item in parsed_array 
                        if isinstance(item, dict) and "symbol" in item
                    ]
                    return [s for s in symbols if s]
                    
                # Regex fallback
                fallback_symbols = re.findall(r'\\?"symbol\\?":\s*\\?"([A-Z]+)\\?"', array_str)
                return list(dict.fromkeys(fallback_symbols))
            
            print(f"Error: Received status code {response.status_code} for '{pattern_name}' (Attempt {attempt}).", file=sys.stderr)
            
        except Exception as e:
            print(f"Attempt {attempt} failed for '{pattern_name}': {e}", file=sys.stderr)
            
        if attempt <= retries:
            time.sleep(backoff * attempt)
            
    print(f"Failed to fetch tickers for '{pattern_name}' after {retries + 1} attempts.", file=sys.stderr)
    return []

if __name__ == "__main__":
    # Check command line arguments
    args = sys.argv[1:]
    
    if args and args[0] != 'all':
        # Fetch only a specific pattern specified by the user
        pattern_name = args[0]
        # Construct the URL if only the name was provided (e.g. 'vcp')
        if pattern_name.startswith('http'):
            url = pattern_name
            pattern_name = url.split('/')[-1]
        else:
            url = f"{PATTERN_INDEX_URL}/{pattern_name}"
            
        tickers = fetch_tickers_for_url(url, pattern_name)
        if tickers:
            print(",".join(tickers))
            sys.exit(0)
        else:
            print(f"Failed to fetch tickers for pattern '{pattern_name}'", file=sys.stderr)
            sys.exit(1)
    else:
        # Fetch all patterns
        patterns = discover_patterns()
        if not patterns:
            print("No scanner patterns discovered.", file=sys.stderr)
            sys.exit(1)
            
        print(f"Found {len(patterns)} patterns: {', '.join([p[0] for p in patterns])}", file=sys.stderr)
        
        all_results = {}
        for name, url in patterns:
            tickers = fetch_tickers_for_url(url, name)
            all_results[name] = tickers
            
        # Print results in formatted lines
        print("\n--- EXTRACTED TICKERS BY PATTERN ---")
        for name, tickers in all_results.items():
            print(f"{name}: {','.join(tickers)}")
            
        # Also write results to a JSON file in the python directory for easy consumption
        output_json_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'all_patterns_tickers.json')
        with open(output_json_path, 'w', encoding='utf-8') as f:
            json.dump(all_results, f, indent=2)
        print(f"\nSaved all results to: {output_json_path}", file=sys.stderr)
