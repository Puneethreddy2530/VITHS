zones = [
  {'id':0,  'pts':'140,272 160,100 320,220 307,297',    'label':'Gate', 'cat':'entrance'},
  {'id':1,  'pts':'160,100 500,100 500,220 320,220',    'label':'B1', 'cat':'north'},
  {'id':2,  'pts':'500,100 840,100 680,220 500,220',    'label':'B2', 'cat':'north'},
  {'id':3,  'pts':'840,100 860,217 693,297 680,220',    'label':'B3', 'cat':'corner'},
  {'id':4,  'pts':'860,217 880,333 707,373 693,297',    'label':'B4', 'cat':'east'},
  {'id':5,  'pts':'880,333 900,450 720,450 707,373',    'label':'B5', 'cat':'east'},
  {'id':6,  'pts':'900,450 893,548 714,553 720,450',    'label':'B6', 'cat':'east'},
  {'id':7,  'pts':'893,548 887,645 707,656 714,553',    'label':'B7', 'cat':'east'},
  {'id':8,  'pts':'887,645 840,880 680,760 707,656',    'label':'B8', 'cat':'corner'},
  {'id':9,  'pts':'628,880 840,880 680,760 568,760',    'label':'B9', 'cat':'south'},
  {'id':10, 'pts':'373,880 628,880 568,760 433,760 320,760 307,657 140,737 160,880', 'label':'B10', 'cat':'corner'},
  {'id':11, 'pts':'160,880 373,880 433,760 320,760',    'label':'B11', 'cat':'south'},
  {'id':12, 'pts':'140,737 120,593 293,553 307,657',    'label':'B12', 'cat':'west'},
  {'id':13, 'pts':'120,593 100,450 280,450 293,553',    'label':'B13', 'cat':'west'},
  {'id':14, 'pts':'100,450 120,361 293,373 280,450',    'label':'B14', 'cat':'west'},
  {'id':15, 'pts':'120,361 140,272 307,297 293,373',    'label':'B15', 'cat':'corner'}
]

def polygon_centroid(pts):
    x = [float(p.split(',')[0]) for p in pts.split(' ')]
    y = [float(p.split(',')[1]) for p in pts.split(' ')]
    
    A = 0
    Cx = 0
    Cy = 0
    for i in range(len(x)):
        x0, y0 = x[i], y[i]
        x1, y1 = x[(i+1)%len(x)], y[(i+1)%len(x)]
        cross = (x0*y1 - x1*y0)
        A += cross
        Cx += (x0 + x1) * cross
        Cy += (y0 + y1) * cross
        
    A *= 0.5
    if A == 0:
        return 0, 0
    Cx /= (6*A)
    Cy /= (6*A)
    return int(round(Cx)), int(round(Cy))

for z in zones:
    cx, cy = polygon_centroid(z['pts'])
    print(f"  {{ id:{z['id']}, pts:'{z['pts']}', label:'{z['label']}', cat:'{z['cat']}', cx:{cx}, cy:{cy} }},")
