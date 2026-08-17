import hmac
import hashlib
import time
from urllib.parse import parse_qsl

INIT_DATA_MAX_AGE_SECONDS = 3600

def verify_telegram_web_app_data(init_data: str, bot_token: str) -> dict | None:
    """
    Verifies the integrity and freshness of Telegram Web App initData.
    Returns the parsed dictionary if valid, otherwise returns None.
    
    Follows: https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app
    """
    try:
        parsed_data = dict(parse_qsl(init_data, strict_parsing=True))
        
        # We need a copy of the parsed data to extract hash without mutating the return payload
        validation_data = parsed_data.copy()
        
        if 'hash' not in validation_data:
            return None
        received_hash = validation_data.pop('hash')
        
        if 'auth_date' not in validation_data:
            return None
            
        auth_date = int(validation_data['auth_date'])
        if time.time() - auth_date > INIT_DATA_MAX_AGE_SECONDS:
            return None
            
        data_check_string = '\n'.join(
            f"{k}={v}" for k, v in sorted(validation_data.items())
        )
        
        secret_key = hmac.new(
            key=b"WebAppData",
            msg=bot_token.encode('utf-8'),
            digestmod=hashlib.sha256
        ).digest()
        
        computed_hash = hmac.new(
            key=secret_key,
            msg=data_check_string.encode('utf-8'),
            digestmod=hashlib.sha256
        ).hexdigest()
        
        if hmac.compare_digest(computed_hash, received_hash):
            return parsed_data  # Return the original parsed dict (including hash)
        return None
        
    except Exception:
        return None
