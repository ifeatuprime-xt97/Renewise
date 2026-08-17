import urllib.request
import json
req = urllib.request.Request('https://testnet.toncenter.com/api/v3/transactions?limit=1', headers={'User-Agent': 'Mozilla/5.0'})
res = urllib.request.urlopen(req)
data = json.loads(res.read())
print(json.dumps(data['transactions'][0], indent=2))
