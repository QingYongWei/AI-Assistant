from app.security import is_high_risk

def test_high_risk_detection():
    assert is_high_risk('git push origin main') is True
    assert is_high_risk('git reset --hard HEAD') is True
    assert is_high_risk('rm -rf build') is True
    assert is_high_risk('npm test') is False
    assert is_high_risk('node verify.js') is False
import pytest
from app.security import check_command,PolicyViolation
def test_high_risk_requires_approval():
    with pytest.raises(PolicyViolation): check_command('git push origin main','.')
def test_safe_command(): assert check_command('python --version','.')

