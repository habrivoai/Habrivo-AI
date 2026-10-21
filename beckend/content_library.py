"""Customer-owned marketing packs. No database connections or DDL on import."""
from contextlib import contextmanager
from datetime import datetime, timezone
from functools import wraps
import hashlib
import json
import re

from flask import Blueprint, jsonify, request, session
from itsdangerous import BadData, URLSafeTimedSerializer
from marketing_studio import OUTPUTS, validate, StudioError

PAGE_SIZE = 20
ITEM_LIMIT = 200
MAX_BODY = 32768
COLUMNS = 'id, product_name, details_json, outputs_json, created_at'


class LibraryError(Exception):
    def __init__(self, code, message, status):
        super().__init__(message)
        self.code, self.status = code, status


def pack_data(details, outputs):
    try:
        clean = validate(details)
    except StudioError:
        raise LibraryError('invalid_input', 'Product details are invalid.', 400) from None
    if not isinstance(outputs, dict) or set(outputs) != set(OUTPUTS):
        raise LibraryError('invalid_input', 'Provide all four marketing outputs.', 400)
    for key, maximum in OUTPUTS.items():
        text = outputs[key]
        if (not isinstance(text, str) or not text.strip() or len(text) > maximum
                or re.search(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', text)):
            raise LibraryError('invalid_input', 'Marketing outputs are invalid or too long.', 400)
    try:
        for text in (*clean.values(), *outputs.values()):
            text.encode('utf-8')
    except UnicodeError:
        raise LibraryError('invalid_input', 'Content must contain valid Unicode text.', 400) from None
    # Exact generated output strings are preserved, including whitespace.
    canonical = json.dumps({'details': clean, 'outputs': outputs}, sort_keys=True,
                           separators=(',', ':'), ensure_ascii=True)
    return clean, hashlib.sha256(canonical.encode('utf-8')).hexdigest()


def save_token(secret, customer_id, details, outputs):
    _, digest = pack_data(details, outputs)
    return URLSafeTimedSerializer(secret, salt='marketing-library-v1').dumps(
        {'customer_id': customer_id, 'digest': digest})


def create_blueprint(db_factory, config, secret):
    bp = Blueprint('content_library', __name__)

    def authenticated(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if not session.get('customer_id'):
                raise LibraryError('unauthorized', 'Please sign in first.', 401)
            return view(*args, **kwargs)
        return wrapped

    @contextmanager
    def owned_db():
        db = db_factory()
        try:
            if not db.execute('SELECT id FROM users WHERE id = ?', (session['customer_id'],)).fetchone():
                raise LibraryError('unauthorized', 'Please sign in again.', 401)
            yield db
        finally:
            db.close()

    def item(row):
        return {'id': row['id'], 'product_name': row['product_name'],
                'details': json.loads(row['details_json']), 'outputs': json.loads(row['outputs_json']),
                'created_at': row['created_at']}

    @bp.after_request
    def no_cache(response):
        response.headers['Cache-Control'] = 'no-store'
        return response

    @bp.errorhandler(LibraryError)
    def expected(error):
        return jsonify(success=False, code=error.code, error=str(error)), error.status

    @bp.errorhandler(Exception)
    def unavailable(error):
        # Missing migration, storage and parsing errors never reveal SQL/records.
        return jsonify(success=False, code='library_unavailable',
                       error='Content library is temporarily unavailable. Please try again.'), 503

    @bp.post('/api/marketing/library')
    @authenticated
    def save():
        if request.mimetype != 'application/json':
            raise LibraryError('invalid_input', 'Send the marketing pack as JSON.', 400)
        if request.content_length is not None and request.content_length > MAX_BODY:
            raise LibraryError('too_large', 'The marketing pack exceeds the size limit.', 413)
        raw = request.stream.read(MAX_BODY + 1)
        if len(raw) > MAX_BODY:
            raise LibraryError('too_large', 'The marketing pack exceeds the size limit.', 413)
        try:
            data = json.loads(raw)
        except (ValueError, UnicodeError):
            raise LibraryError('invalid_input', 'Provide a valid marketing pack.', 400) from None
        if not isinstance(data, dict) or set(data) != {'details', 'outputs', 'save_token'}:
            raise LibraryError('invalid_input', 'Provide product details, outputs and the save proof.', 400)
        if not isinstance(data['save_token'], str) or len(data['save_token']) > 2048:
            raise LibraryError('invalid_input', 'The save proof is invalid.', 400)
        clean, digest = pack_data(data['details'], data['outputs'])
        try:
            proof = URLSafeTimedSerializer(secret, salt='marketing-library-v1').loads(data['save_token'], max_age=86400)
            if not isinstance(proof, dict) or proof.get('customer_id') != session['customer_id'] or proof.get('digest') != digest:
                raise BadData('mismatch')
        except BadData:
            raise LibraryError('invalid_proof', 'The save proof is invalid or expired. Your displayed content can still be copied.', 400) from None
        customer_id = session['customer_id']
        with owned_db() as db:
            with db:
                if config.backend == 'sqlite':
                    db.execute('BEGIN IMMEDIATE')
                else:
                    db.execute("SET LOCAL lock_timeout = '3s'")
                    db.execute('SELECT pg_advisory_xact_lock(724182605)')
                existing = db.execute('SELECT ' + COLUMNS + ' FROM marketing_content WHERE customer_id = ? AND content_digest = ?',
                                      (customer_id, digest)).fetchone()
                created = existing is None
                if created:
                    count = db.execute('SELECT COUNT(*) FROM marketing_content WHERE customer_id = ?', (customer_id,)).fetchone()[0]
                    if count >= ITEM_LIMIT:
                        raise LibraryError('library_full', 'Your library is full (200 packs). Delete an item before saving another.', 409)
                    db.execute('''INSERT INTO marketing_content
                        (customer_id, content_digest, product_name, details_json, outputs_json, created_at)
                        VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (customer_id, content_digest) DO NOTHING''',
                        (customer_id, digest, clean['product_name'], json.dumps(clean), json.dumps(data['outputs']),
                         datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')))
                    existing = db.execute('SELECT ' + COLUMNS + ' FROM marketing_content WHERE customer_id = ? AND content_digest = ?',
                                          (customer_id, digest)).fetchone()
                result = item(existing)
        return jsonify(success=True, item=result, created=created), 201 if created else 200

    @bp.get('/api/marketing/library')
    @authenticated
    def listing():
        if set(request.args) - {'before'} or len(request.args.getlist('before')) > 1:
            raise LibraryError('invalid_cursor', 'Invalid library page.', 400)
        cursor = request.args.get('before')
        if cursor is not None and (len(cursor) > 19 or not cursor.isascii() or not cursor.isdecimal() or not 0 < int(cursor) < 2**63):
            raise LibraryError('invalid_cursor', 'Invalid library page.', 400)
        with owned_db() as db:
            if cursor is None:
                rows = db.execute('SELECT ' + COLUMNS + ' FROM marketing_content WHERE customer_id = ? ORDER BY id DESC LIMIT ?',
                                  (session['customer_id'], PAGE_SIZE + 1)).fetchall()
            else:
                rows = db.execute('SELECT ' + COLUMNS + ' FROM marketing_content WHERE customer_id = ? AND id < ? ORDER BY id DESC LIMIT ?',
                                  (session['customer_id'], int(cursor), PAGE_SIZE + 1)).fetchall()
            items = [item(row) for row in rows[:PAGE_SIZE]]
        return jsonify(success=True, items=items,
                       next_cursor=items[-1]['id'] if len(rows) > PAGE_SIZE else None)

    @bp.get('/api/marketing/library/<int:item_id>')
    @authenticated
    def detail(item_id):
        if not 0 < item_id < 2**63:
            raise LibraryError('not_found', 'Saved item not found.', 404)
        with owned_db() as db:
            row = db.execute('SELECT ' + COLUMNS + ' FROM marketing_content WHERE id = ? AND customer_id = ?',
                             (item_id, session['customer_id'])).fetchone()
            if row is None:
                raise LibraryError('not_found', 'Saved item not found.', 404)
            result = item(row)
        return jsonify(success=True, item=result)

    @bp.delete('/api/marketing/library/<int:item_id>')
    @authenticated
    def delete(item_id):
        if not 0 < item_id < 2**63:
            raise LibraryError('not_found', 'Saved item not found.', 404)
        with owned_db() as db:
            with db:
                deleted = db.execute('DELETE FROM marketing_content WHERE id = ? AND customer_id = ?',
                                     (item_id, session['customer_id'])).rowcount
                if deleted != 1:
                    raise LibraryError('not_found', 'Saved item not found.', 404)
        return jsonify(success=True)

    return bp
