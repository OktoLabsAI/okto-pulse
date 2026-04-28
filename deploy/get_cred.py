import sys
CREDS = '/home/maheidem/server-management/.claude.local.md'
lines = open(CREDS).read().splitlines()
field = sys.argv[1]
if field == 'api_key':
    line = next(l for l in lines if 'read-only' in l)
elif field == 'password':
    line = next(l for l in lines if 'Admin Password' in l)
else:
    sys.exit(1)
# Extract content between first pair of backticks on the line
parts = line.split('`')
print(parts[1] if len(parts) > 2 else '', end='')
