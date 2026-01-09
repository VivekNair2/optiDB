import re
from datetime import datetime

LOG_PATTERN = re.compile(
    r'(?P<time>[\d\-:. ]+) IST \[\d+\] (?P<user>\w+)@(?P<db>\w+) LOG:\s+'
    r'duration: (?P<duration>[\d.]+) ms\s+statement: (?P<query>.+)',
    re.DOTALL
)

def parse_logs(log_file):
    rows = []
    try:
        with open(log_file, 'r', encoding='utf-8') as f:
            content = f.read()
        
        # Split by LOG entries to handle multi-line queries
        # Find all duration log entries
        matches = re.finditer(
            r'(?P<time>[\d\-:. ]+) IST \[\d+\] (?P<user>\w+)@(?P<db>\w+) LOG:\s+'
            r'duration: (?P<duration>[\d.]+) ms\s+statement:\s*(?P<query>.*?)(?=\n\d{4}-\d{2}-\d{2}|\Z)',
            content,
            re.DOTALL
        )
        
        for match in matches:
            query = match.group("query").strip()
            # Clean up the query - remove excessive whitespace
            query = ' '.join(query.split())
            
            if query:  # Only add if query is not empty
                rows.append({
                    "timestamp": match.group("time"),
                    "user": match.group("user"),
                    "database": match.group("db"),
                    "duration_ms": float(match.group("duration")),
                    "query": query
                })
    except Exception as e:
        print(f"Error parsing logs: {e}")
    
    return rows
