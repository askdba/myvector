import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mysql.connector

DB = dict(host=os.getenv('MYSQL_HOST','localhost'), port=int(os.getenv('MYSQL_PORT','3306')),
          user=os.getenv('MYSQL_USER','root'), password=os.getenv('MYSQL_PASSWORD','myvector'),
          database=os.getenv('MYSQL_DATABASE','vectordb'))

PRODUCTS = [
 ('RainShell Pro','Waterproof breathable hiking jacket with taped seams, hood and packable design.'),
 ('TrailWarm Fleece','Warm lightweight fleece midlayer for cool mountain hikes and outdoor travel.'),
 ('StormPack 28','Weather-resistant daypack with laptop sleeve and rain cover for hiking and commuting.'),
 ('Camp Brew Kit','Compact coffee brewer, insulated mug and hand grinder for coffee outdoors.'),
 ('TravelCharge 65','Small 65W USB-C charger with two ports for phones, tablets and laptops while travelling.'),
 ('Nomad Power Bank','Slim high-capacity USB-C battery pack for long flights, camping and remote work.'),
 ('Alpine Gloves','Insulated touchscreen gloves designed for cold-weather hiking and cycling.'),
 ('City Raincoat','Lightweight hooded raincoat for daily commuting and wet-weather city walks.'),
]

# Deterministic demo vectors. The app maps intent phrases into a few semantic dimensions.
# This keeps the booth self-contained; replace this seed with real embeddings for a production demo.
V = {
 'RainShell Pro':[1.0,1.0,.2,.1,.3], 'TrailWarm Fleece':[.1,.9,.8,.2,.5], 'StormPack 28':[.8,.2,.3,.7,.4],
 'Camp Brew Kit':[.1,.1,.9,.8,.2], 'TravelCharge 65':[.1,.1,.2,.9,1.0], 'Nomad Power Bank':[.1,.1,.2,.8,.9],
 'Alpine Gloves':[.2,1.0,.7,.1,.4], 'City Raincoat':[.9,.4,.1,.4,.2]
}

def vector(q):
 q=q.lower()
 return [
  1.0 if any(x in q for x in ['rain','waterproof','wet']) else 0.0,
  1.0 if any(x in q for x in ['warm','cold','winter','hike','mountain']) else 0.0,
  1.0 if any(x in q for x in ['coffee','brew','outdoor','camp']) else 0.0,
  1.0 if any(x in q for x in ['travel','flight','charger','pack','commute']) else 0.0,
  1.0 if any(x in q for x in ['phone','laptop','usb','power','charge']) else 0.0,
 ]

def search(q):
 qv=vector(q)
 scored=[]
 for name,desc in PRODUCTS:
  v=V[name]
  score=sum(a*b for a,b in zip(qv,v))
  scored.append((score,name,desc))
 return sorted(scored, reverse=True)[:5]

def init_db():
 conn=mysql.connector.connect(**DB)
 cur=conn.cursor()
 cur.execute('CREATE TABLE IF NOT EXISTS products (id INT PRIMARY KEY AUTO_INCREMENT, name VARCHAR(255), description TEXT)')
 cur.execute('SELECT COUNT(*) FROM products')
 if cur.fetchone()[0]==0:
  cur.executemany('INSERT INTO products(name,description) VALUES(%s,%s)', PRODUCTS)
  conn.commit()
 cur.close(); conn.close()

HTML='''<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>MyVector Booth Demo</title><style>
body{font-family:system-ui;margin:0;background:#0b1020;color:#f7f8fb}.wrap{max-width:1050px;margin:auto;padding:48px 28px}.badge{color:#8ee6c0;font-weight:700}.hero{font-size:54px;line-height:1;margin:12px 0 18px}.sub{color:#aab3c5;font-size:20px;max-width:760px}.search{display:flex;gap:10px;margin:32px 0}.search input{flex:1;padding:20px;border-radius:12px;border:1px solid #39445d;background:#151c31;color:white;font-size:20px}.search button,.chip{padding:14px 18px;border:0;border-radius:10px;background:#8ee6c0;color:#071018;font-weight:800;cursor:pointer}.chips{display:flex;gap:8px;flex-wrap:wrap}.chip{background:#202a43;color:#dce4f2}.card{background:#121a2d;border:1px solid #293650;border-radius:16px;padding:20px;margin:12px 0}.score{float:right;color:#8ee6c0;font-weight:800}.meta{color:#8f9bb2}.reveal{margin-top:30px}.sql{background:#070b14;padding:18px;border-radius:12px;overflow:auto;color:#c9d5eb;display:none}</style></head><body><div class="wrap">
<div class="badge">MYSQL + MYVECTOR • CONFERENCE BOOTH</div><div class="hero">Search by meaning.</div><div class="sub">Ask for what you want in natural language. MyVector finds semantically relevant products using an HNSW vector index inside MySQL.</div>
<div class="search"><input id="q" placeholder="Try: something warm for a rainy hike"><button onclick="go()">Search</button></div><div class="chips"><button class="chip" onclick="demo('something warm for a rainy hike')">Rainy hike</button><button class="chip" onclick="demo('gift for someone who loves coffee')">Coffee gift</button><button class="chip" onclick="demo('lightweight travel charger')">Travel power</button></div><div id="results"></div>
<div class="reveal"><button class="chip" onclick="document.querySelector('.sql').style.display='block'">Show the SQL</button><pre class="sql">SELECT id, name, description
FROM products
WHERE MYVECTOR_IS_ANN(
  'products.vec', 'id',
  myvector_construct(:query_embedding)
);</pre></div></div><script>
async function go(){let q=document.getElementById('q').value;let r=await fetch('/search?q='+encodeURIComponent(q));let d=await r.json();document.getElementById('results').innerHTML='<h2>'+d.results.length+' relevant results</h2>'+d.results.map(x=>`<div class="card"><span class="score">${x.score.toFixed(2)}</span><h2>${x.name}</h2><div>${x.description}</div><div class="meta">Semantic match</div></div>`).join('')}
function demo(x){document.getElementById('q').value=x;go()} go();</script></body></html>'''

class Handler(BaseHTTPRequestHandler):
 def do_GET(self):
  if self.path.startswith('/search'):
   from urllib.parse import urlparse,parse_qs
   q=parse_qs(urlparse(self.path).query).get('q',[''])[0]
   data={'query':q,'results':[{'name':n,'description':d,'score':s} for s,n,d in search(q)]}
   body=json.dumps(data).encode(); self.send_response(200); self.send_header('Content-Type','application/json'); self.send_header('Content-Length',str(len(body))); self.end_headers(); self.wfile.write(body); return
  body=HTML.encode(); self.send_response(200); self.send_header('Content-Type','text/html'); self.send_header('Content-Length',str(len(body))); self.end_headers(); self.wfile.write(body)

if __name__=='__main__':
 init_db(); ThreadingHTTPServer(('0.0.0.0',int(os.getenv('PORT','8080'))),Handler).serve_forever()
