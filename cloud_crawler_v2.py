import pandas as pd
import matplotlib.pyplot as plt
from datetime import datetime

def generate_period_chart(df, title, filename):
    """Generates and saves a specific trend chart."""
    if df.empty:
        return None
        
    plt.figure(figsize=(10, 6))
    plt.plot(df['crawled_at'], df['price'], marker='o', color='#e91e63', linewidth=2)
    plt.fill_between(df['crawled_at'], df['price'], color='#fce4ec', alpha=0.3)
    
    plt.title(title, fontsize=14, fontweight='bold')
    plt.grid(True, linestyle='--', alpha=0.6)
    plt.xticks(rotation=45)
    plt.tight_layout()
    
    plt.savefig(filename)
    plt.close() # Release memory
    return filename

def export_visuals(sheet_data):
    df = pd.DataFrame(sheet_data)
    df['price'] = pd.to_numeric(df['price'])
    df['crawled_at'] = pd.to_datetime(df['crawled_at'])
    
    now = datetime.now()
    current_year = now.year
    current_month = now.month

    # 1. This Month
    this_month_df = df[(df['crawled_at'].dt.year == current_year) & 
                       (df['crawled_at'].dt.month == current_month)]
    
    # 2. This Year
    this_year_df = df[df['crawled_at'].dt.year == current_year]
    
    # 3. Last Year
    last_year_df = df[df['crawled_at'].dt.year == current_year - 1]

    charts = [
        generate_period_chart(this_month_df, f"Silver Trend: {now.strftime('%B %Y')}", "month.png"),
        generate_period_chart(this_year_df, f"Silver Trend: Year {current_year}", "year.png"),
        generate_period_chart(last_year_df, f"Silver Trend: Year {current_year - 1}", "last_year.png")
    ]
    
    return [c for c in charts if c is not None]