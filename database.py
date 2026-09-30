import sqlite3
import os
import uuid
import json
from datetime import datetime
from typing import Optional, List, Dict, Any

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data', 'leadgen.db')

def init_db() -> None:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with get_db() as conn:
        conn.execute('''
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY,
                type TEXT NOT NULL,
                query TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                config TEXT,
                progress TEXT,
                error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                completed_at TEXT
            )
        ''')
        
        conn.execute('''
            CREATE TABLE IF NOT EXISTS leads (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id TEXT NOT NULL REFERENCES jobs(id),
                name TEXT NOT NULL,
                phone TEXT DEFAULT '',
                whatsapp TEXT DEFAULT '',
                email TEXT DEFAULT '',
                contact_person TEXT DEFAULT '',
                full_address TEXT DEFAULT '',
                area TEXT DEFAULT '',
                city TEXT DEFAULT '',
                pincode TEXT DEFAULT '',
                rating TEXT DEFAULT '',
                total_reviews TEXT DEFAULT '',
                website TEXT DEFAULT '',
                source_url TEXT DEFAULT '',
                source_domain TEXT DEFAULT '',
                created_at TEXT NOT NULL,
                UNIQUE(job_id, name, phone)
            )
        ''')
        
        conn.execute('''
            CREATE TABLE IF NOT EXISTS campaigns (
                id TEXT PRIMARY KEY,
                job_id TEXT NOT NULL REFERENCES jobs(id),
                subject_template TEXT NOT NULL,
                body_template TEXT NOT NULL,
                video_link TEXT DEFAULT '',
                status TEXT NOT NULL DEFAULT 'pending',
                total_recipients INTEGER DEFAULT 0,
                sent_count INTEGER DEFAULT 0,
                failed_count INTEGER DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        ''')
        
        conn.execute('''
            CREATE TABLE IF NOT EXISTS campaign_recipients (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                campaign_id TEXT NOT NULL REFERENCES campaigns(id),
                lead_id INTEGER NOT NULL REFERENCES leads(id),
                email TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                error TEXT DEFAULT '',
                sent_at TEXT,
                UNIQUE(campaign_id, lead_id)
            )
        ''')
        conn.commit()

def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=20.0)
    conn.execute('PRAGMA journal_mode=WAL')
    conn.row_factory = sqlite3.Row
    return conn

# Jobs
def create_job(job_type: str, query: str, config: dict) -> str:
    job_id = str(uuid.uuid4())
    now = datetime.utcnow().isoformat()
    with get_db() as conn:
        conn.execute(
            'INSERT INTO jobs (id, type, query, config, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)',
            (job_id, job_type, query, json.dumps(config), now, now)
        )
        conn.commit()
    return job_id

def update_job_status(job_id: str, status: str, error: str = None) -> None:
    now = datetime.utcnow().isoformat()
    with get_db() as conn:
        if status in ('completed', 'failed', 'cancelled'):
            conn.execute(
                'UPDATE jobs SET status = ?, error = ?, updated_at = ?, completed_at = ? WHERE id = ?',
                (status, error, now, now, job_id)
            )
        else:
            conn.execute(
                'UPDATE jobs SET status = ?, error = ?, updated_at = ? WHERE id = ?',
                (status, error, now, job_id)
            )
        conn.commit()

def update_job_progress(job_id: str, progress: dict) -> None:
    now = datetime.utcnow().isoformat()
    with get_db() as conn:
        conn.execute(
            'UPDATE jobs SET progress = ?, updated_at = ? WHERE id = ?',
            (json.dumps(progress), now, job_id)
        )
        conn.commit()

def get_job(job_id: str) -> Optional[Dict[str, Any]]:
    with get_db() as conn:
        row = conn.execute('SELECT * FROM jobs WHERE id = ?', (job_id,)).fetchone()
        if row:
            job = dict(row)
            if job['config']:
                job['config'] = json.loads(job['config'])
            if job['progress']:
                job['progress'] = json.loads(job['progress'])
            return job
        return None

def get_recent_jobs(limit: int = 20) -> List[Dict[str, Any]]:
    with get_db() as conn:
        rows = conn.execute('''
            SELECT j.*, COUNT(l.id) as lead_count 
            FROM jobs j 
            LEFT JOIN leads l ON j.id = l.job_id 
            GROUP BY j.id 
            ORDER BY j.created_at DESC 
            LIMIT ?
        ''', (limit,)).fetchall()
        jobs = []
        for row in rows:
            job = dict(row)
            if job['config']:
                job['config'] = json.loads(job['config'])
            if job['progress']:
                job['progress'] = json.loads(job['progress'])
            jobs.append(job)
        return jobs

# Leads
def insert_leads(job_id: str, leads: List[Dict[str, Any]]) -> int:
    if not leads:
        return 0
    now = datetime.utcnow().isoformat()
    count = 0
    with get_db() as conn:
        for lead in leads:
            try:
                cursor = conn.execute('''
                    INSERT OR IGNORE INTO leads 
                    (job_id, name, phone, whatsapp, email, contact_person, full_address, 
                    area, city, pincode, rating, total_reviews, website, source_url, source_domain, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (
                    job_id,
                    lead.get('name', ''),
                    lead.get('phone', ''),
                    lead.get('whatsapp', ''),
                    lead.get('email', ''),
                    lead.get('contact_person', ''),
                    lead.get('full_address', ''),
                    lead.get('area', ''),
                    lead.get('city', ''),
                    lead.get('pincode', ''),
                    lead.get('rating', ''),
                    lead.get('total_reviews', ''),
                    lead.get('website', ''),
                    lead.get('source_url', ''),
                    lead.get('source_domain', ''),
                    now
                ))
                count += cursor.rowcount
            except sqlite3.Error as e:
                pass
        conn.commit()
    return count

def get_leads(job_id: str) -> List[Dict[str, Any]]:
    with get_db() as conn:
        rows = conn.execute('SELECT * FROM leads WHERE job_id = ? ORDER BY id', (job_id,)).fetchall()
        return [dict(row) for row in rows]

def get_leads_with_email(job_id: str) -> List[Dict[str, Any]]:
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM leads WHERE job_id = ? AND email != '' AND email IS NOT NULL ORDER BY id", (job_id,)).fetchall()
        return [dict(row) for row in rows]

# Campaigns
def create_campaign(job_id: str, subject: str, body: str, video_link: str, lead_ids: List[int]) -> str:
    campaign_id = str(uuid.uuid4())
    now = datetime.utcnow().isoformat()
    total_recipients = len(lead_ids)
    
    with get_db() as conn:
        conn.execute('''
            INSERT INTO campaigns (id, job_id, subject_template, body_template, video_link, total_recipients, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ''', (campaign_id, job_id, subject, body, video_link, total_recipients, now, now))
        
        for lead_id in lead_ids:
            # get email for lead
            lead_row = conn.execute('SELECT email FROM leads WHERE id = ?', (lead_id,)).fetchone()
            if lead_row:
                email = lead_row['email']
                conn.execute('''
                    INSERT INTO campaign_recipients (campaign_id, lead_id, email)
                    VALUES (?, ?, ?)
                ''', (campaign_id, lead_id, email))
        conn.commit()
    return campaign_id

def update_campaign_status(campaign_id: str, status: str, sent: int = None, failed: int = None) -> None:
    now = datetime.utcnow().isoformat()
    with get_db() as conn:
        updates = ['status = ?', 'updated_at = ?']
        params = [status, now]
        if sent is not None:
            updates.append('sent_count = ?')
            params.append(sent)
        if failed is not None:
            updates.append('failed_count = ?')
            params.append(failed)
            
        params.append(campaign_id)
        conn.execute(f"UPDATE campaigns SET {', '.join(updates)} WHERE id = ?", params)
        conn.commit()

def get_campaign(campaign_id: str) -> Optional[Dict[str, Any]]:
    with get_db() as conn:
        row = conn.execute('SELECT * FROM campaigns WHERE id = ?', (campaign_id,)).fetchone()
        if row:
            return dict(row)
        return None

def mark_recipient_sent(campaign_id: str, lead_id: int) -> None:
    now = datetime.utcnow().isoformat()
    with get_db() as conn:
        conn.execute('''
            UPDATE campaign_recipients 
            SET status = 'sent', sent_at = ? 
            WHERE campaign_id = ? AND lead_id = ?
        ''', (now, campaign_id, lead_id))
        conn.commit()

def mark_recipient_failed(campaign_id: str, lead_id: int, error: str) -> None:
    with get_db() as conn:
        conn.execute('''
            UPDATE campaign_recipients 
            SET status = 'failed', error = ? 
            WHERE campaign_id = ? AND lead_id = ?
        ''', (error, campaign_id, lead_id))
        conn.commit()

def get_pending_recipients(campaign_id: str) -> List[Dict[str, Any]]:
    with get_db() as conn:
        rows = conn.execute('''
            SELECT * FROM campaign_recipients 
            WHERE campaign_id = ? AND status = 'pending'
            ORDER BY id
        ''', (campaign_id,)).fetchall()
        return [dict(row) for row in rows]

def is_lead_already_sent(campaign_id: str, lead_id: int) -> bool:
    with get_db() as conn:
        row = conn.execute('''
            SELECT status FROM campaign_recipients
            WHERE campaign_id = ? AND lead_id = ?
        ''', (campaign_id, lead_id)).fetchone()
        
        if row and row['status'] == 'sent':
            return True
        return False
