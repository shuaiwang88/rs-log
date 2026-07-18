import pandas as pd
from io import StringIO

# Read the file content
with open('/mnt/agents/upload/Breakaway Gap.txt', 'r') as f:
    content = f.read()

# Parse the CSV data
df = pd.read_csv(StringIO(content))

# Clean column names (remove any leading/trailing whitespace)
df.columns = df.columns.str.strip()

# Sort by 'Ind Group Rank' (ascending - lower rank is better), then by 'Comp Rating' (descending - higher rating is better)
df_sorted = df.sort_values(by=['Ind Group Rank', 'Comp Rating'], ascending=[True, False])

# Extract symbols as a comma-separated list
symbols_list = ', '.join(df_sorted['Symbol'].tolist())

print("Symbols sorted by Industry Group Rank (ascending), then IBD Comp Rating (descending):")
print(symbols_list)
print(f"\nTotal symbols: {len(df_sorted)}")

