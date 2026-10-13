"""Real MariaDB object visibility and fail-closed inventory, isolated schema only."""
import json
import hashlib
import pathlib
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "tools"))
from fixture_environment import child_environment


def check_visibility(work, socket_path):
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / 'src'))
    from paymenter_backups.common import BackupError
    from paymenter_backups.engine import defaults_file, inventory
    mysql = '/usr/bin/mariadb'
    def sql(statement, user='root', password=None):
        command = [mysql, '--no-defaults', '--socket=' + str(socket_path), '-u' + user, '--batch', '--skip-column-names']
        if password:
            # Fake fixture password never represents a deployment secret.
            command.append('-p' + password)
        result = subprocess.run(command, input=statement.encode(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True, env=child_environment())
        return result.stdout.decode()
    sql("CREATE DATABASE inventory_fixture; CREATE USER 'inventory_restricted'@'localhost' IDENTIFIED BY 'fixture-only'; GRANT SELECT,INSERT,UPDATE,DELETE,CREATE,ALTER,INDEX,DROP ON inventory_fixture.* TO 'inventory_restricted'@'localhost'; CREATE TABLE inventory_fixture.sample (id INT PRIMARY KEY) ENGINE=InnoDB;")
    envelope = {'database': {'driver': 'mariadb', 'name': 'inventory_fixture', 'user': 'inventory_restricted', 'password': 'fixture-only', 'socket': str(socket_path)}, 'tools': {'mysql': mysql}}
    defaults = work / 'inventory.cnf'
    defaults_file(defaults, envelope['database'])
    baseline = inventory(envelope, defaults, 10, lambda group: None)
    auth_hash = hashlib.sha1(hashlib.sha1(b'fixture-only').digest()).hexdigest().upper()
    if auth_hash in json.dumps(baseline) or 'fixture-only' in json.dumps(baseline):
        raise RuntimeError('inventory authentication hash was not redacted')
    if baseline['state'] != 'visible-zero' or not baseline['current_user_grants']:
        raise RuntimeError('restricted visible-zero baseline failed')
    sql("CREATE PROCEDURE inventory_fixture.fixture_routine() SELECT 1; CREATE EVENT inventory_fixture.fixture_event ON SCHEDULE EVERY 1 DAY DO INSERT INTO inventory_fixture.sample VALUES (999); CREATE TRIGGER inventory_fixture.fixture_trigger BEFORE INSERT ON inventory_fixture.sample FOR EACH ROW SET NEW.id=NEW.id; CREATE VIEW inventory_fixture.fixture_view AS SELECT id FROM inventory_fixture.sample;")
    query = "SELECT 'routines',COUNT(*) FROM information_schema.ROUTINES WHERE ROUTINE_SCHEMA='inventory_fixture'; SELECT 'events',COUNT(*) FROM information_schema.EVENTS WHERE EVENT_SCHEMA='inventory_fixture'; SELECT 'triggers',COUNT(*) FROM information_schema.TRIGGERS WHERE TRIGGER_SCHEMA='inventory_fixture'; SELECT 'views',COUNT(*) FROM information_schema.TABLES WHERE TABLE_SCHEMA='inventory_fixture' AND TABLE_TYPE='VIEW';"
    def counts(user, password=None):
        return dict((name, int(value)) for name, value in (line.split('\t') for line in sql(query, user, password).splitlines()))
    root_counts = counts('root')
    restricted_counts = counts('inventory_restricted', 'fixture-only')
    if root_counts != {'routines': 1, 'events': 1, 'triggers': 1, 'views': 1}:
        raise RuntimeError('root fixture object inventory incomplete')
    if restricted_counts != {'routines': 0, 'events': 0, 'triggers': 0, 'views': 1}:
        raise RuntimeError('restricted MariaDB visibility differs; investigate and update disclosed fixture contract: ' + json.dumps(restricted_counts))
    try:
        inventory(envelope, defaults, 10, lambda group: None)
        raise RuntimeError('visible nonzero view admitted')
    except BackupError as error:
        if error.code != 'unsupported-db-objects': raise
    sql('DROP VIEW inventory_fixture.fixture_view;')
    hidden = inventory(envelope, defaults, 10, lambda group: None)
    if hidden['state'] != 'visible-zero' or 'hidden objects are unknown' not in hidden['scope']:
        raise RuntimeError('limited-visibility manifest scope missing')
    bad = work / 'invalid-inventory.cnf'
    defaults_file(bad, {**envelope['database'], 'password': 'deliberately-wrong'})
    try:
        inventory(envelope, bad, 10, lambda group: None)
        raise RuntimeError('inventory query error admitted')
    except BackupError as error:
        if error.code != 'database-inventory-failed': raise
    defaults.unlink(); bad.unlink()
    sql("DROP DATABASE inventory_fixture; DROP USER 'inventory_restricted'@'localhost';")
    return {'root': root_counts, 'restricted': restricted_counts, 'baseline': 'visible-zero', 'with_view': 'visible-nonzero/refused', 'hidden_objects': 'visible-zero/disclosed', 'query_error': 'refused'}
