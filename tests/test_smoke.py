import uuid
from fastapi.testclient import TestClient
from app.main import app

def test_health():
    with TestClient(app) as client:
        r=client.get('/health'); assert r.status_code==200; assert r.json()['product']=='ASHA AI'

def test_household_and_timeline():
    with TestClient(app) as client:
        h=client.post('/households',json={'external_id':'HH-TEST-'+uuid.uuid4().hex[:8],'village_id':'V1'}); assert h.status_code==200
        hid=h.json()['id']
        e=client.post('/events',json={'external_id':'EV-TEST-'+uuid.uuid4().hex[:8],'household_id':hid,'event_type':'home_visit','event_date':'2026-10-06','details':{'note':'test'}})
        assert e.status_code==200
        t=client.get(f'/households/{hid}/timeline'); assert t.status_code==200; assert t.json()[0]['event_type']=='home_visit'
