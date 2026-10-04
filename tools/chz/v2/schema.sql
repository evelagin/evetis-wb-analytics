-- Offline schema 2. No import or migration of schema 1 / production state.
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS metadata(
 environment TEXT NOT NULL CHECK(environment IN ('SANDBOX','PROD')),
 participant TEXT NOT NULL, schema_version INTEGER NOT NULL CHECK(schema_version=2),
 singleton INTEGER NOT NULL DEFAULT 1 UNIQUE CHECK(singleton=1), UNIQUE(environment,participant));
CREATE TABLE IF NOT EXISTS operations(
 id TEXT PRIMARY KEY NOT NULL CHECK(length(id)=64 AND id NOT GLOB '*[^0-9a-f]*'),
 fingerprint TEXT NOT NULL CHECK(length(fingerprint)=64 AND fingerprint NOT GLOB '*[^0-9a-f]*'),
 payload_hash TEXT NOT NULL CHECK(length(payload_hash)=64 AND payload_hash NOT GLOB '*[^0-9a-f]*'),
 environment TEXT NOT NULL, participant TEXT NOT NULL,
 kind TEXT NOT NULL CHECK(kind IN ('ORDER','RETRIEVE','UTILISATION','INTRODUCTION','SET','NK_FEED','NK_SIGN')),
 demand TEXT NOT NULL, payload BLOB NOT NULL CHECK(json_valid(payload)),
 state TEXT NOT NULL CHECK(state IN ('PREPARED','APPROVED','SUBMITTING','UNKNOWN','RECONCILING','ACCEPTED','SUCCEEDED','REJECTED','PARTIAL','INVALIDATED','CANCELLED')),
 created_at INTEGER NOT NULL CHECK(typeof(created_at)='integer' AND created_at>=0),
 UNIQUE(id,payload_hash,environment,participant), FOREIGN KEY(environment,participant) REFERENCES metadata(environment,participant));
CREATE TABLE IF NOT EXISTS approvals(
 id TEXT PRIMARY KEY NOT NULL CHECK(length(id)=32 AND id NOT GLOB '*[^0-9a-f]*'),
 operation_id TEXT NOT NULL, payload_hash TEXT NOT NULL, environment TEXT NOT NULL, participant TEXT NOT NULL,
 owner TEXT NOT NULL, created_at INTEGER NOT NULL CHECK(typeof(created_at)='integer' AND created_at>=0),
 expires_at INTEGER NOT NULL CHECK(typeof(expires_at)='integer' AND expires_at>created_at),
 duplicate_review TEXT NOT NULL CHECK(json_valid(duplicate_review)), consumed INTEGER NOT NULL DEFAULT 0 CHECK(consumed IN (0,1)),
 UNIQUE(id,operation_id,payload_hash,environment,participant),
 FOREIGN KEY(operation_id,payload_hash,environment,participant) REFERENCES operations(id,payload_hash,environment,participant));
CREATE TABLE IF NOT EXISTS attempts(
 id TEXT PRIMARY KEY NOT NULL CHECK(length(id)=32 AND id NOT GLOB '*[^0-9a-f]*'),
 operation_id TEXT NOT NULL REFERENCES operations(id), approval_id TEXT NOT NULL UNIQUE,
 state TEXT NOT NULL CHECK(state IN ('SUBMITTING','UNKNOWN','RECONCILING','ACCEPTED','SUCCEEDED','REJECTED','PARTIAL')),
 submitted_at INTEGER NOT NULL CHECK(typeof(submitted_at)='integer' AND submitted_at>=0), external_id TEXT,
 business_hash TEXT NOT NULL, transport_hash TEXT NOT NULL CHECK(length(transport_hash)=64 AND transport_hash NOT GLOB '*[^0-9a-f]*'),
 signed_hash TEXT NOT NULL CHECK(length(signed_hash)=64 AND signed_hash NOT GLOB '*[^0-9a-f]*'),
 wire_hash TEXT NOT NULL CHECK(length(wire_hash)=64 AND wire_hash NOT GLOB '*[^0-9a-f]*'),
 wire BLOB NOT NULL, baseline TEXT NOT NULL CHECK(json_valid(baseline)), evidence TEXT CHECK(evidence IS NULL OR json_valid(evidence)),
 environment TEXT NOT NULL, participant TEXT NOT NULL,
 CHECK(json_valid(wire) AND json_extract(wire,'$.attempt_id') IS id),
 CHECK(external_id IS NULL OR (external_id GLOB 'SYN:external-[0-9]*' AND substr(external_id,14) NOT GLOB '*[^0-9]*')),
 UNIQUE(id,operation_id), FOREIGN KEY(approval_id,operation_id,business_hash,environment,participant)
 REFERENCES approvals(id,operation_id,payload_hash,environment,participant));
CREATE UNIQUE INDEX IF NOT EXISTS one_active_attempt ON attempts(operation_id) WHERE state<>'REJECTED';
CREATE TABLE IF NOT EXISTS validated_evidence(
 id TEXT PRIMARY KEY NOT NULL, operation_id TEXT NOT NULL, attempt_id TEXT NOT NULL,
 phase TEXT NOT NULL CHECK(phase IN ('SUBMISSION','RECONCILIATION')),
 outcome TEXT NOT NULL CHECK(outcome IN ('ACCEPTED','SUCCEEDED','REJECTED','PARTIAL')),
 body TEXT NOT NULL CHECK(json_valid(body)), response_hash TEXT NOT NULL CHECK(length(response_hash)=64 AND response_hash NOT GLOB '*[^0-9a-f]*'),
 observed_at INTEGER NOT NULL CHECK(typeof(observed_at)='integer' AND observed_at>=0),
 FOREIGN KEY(attempt_id,operation_id) REFERENCES attempts(id,operation_id));
CREATE TABLE IF NOT EXISTS events(
 seq INTEGER PRIMARY KEY AUTOINCREMENT, operation_id TEXT NOT NULL, attempt_id TEXT,
 environment TEXT NOT NULL CHECK(environment IN ('SANDBOX','PROD')),
 timestamp INTEGER NOT NULL CHECK(typeof(timestamp)='integer' AND timestamp>=0), transition TEXT NOT NULL,
 details TEXT NOT NULL CHECK(json_valid(details)), previous_hash TEXT NOT NULL, event_hash TEXT NOT NULL,
 CHECK(length(event_hash)=64 AND event_hash NOT GLOB '*[^0-9a-f]*'),
 CHECK(previous_hash='' OR (length(previous_hash)=64 AND previous_hash NOT GLOB '*[^0-9a-f]*')),
 FOREIGN KEY(attempt_id,operation_id) REFERENCES attempts(id,operation_id));
CREATE TABLE IF NOT EXISTS codes(
 id TEXT PRIMARY KEY NOT NULL, gtin TEXT NOT NULL, position INTEGER NOT NULL UNIQUE CHECK(typeof(position)='integer' AND position>0),
 external_status TEXT NOT NULL CHECK(external_status IN ('EMITTED','APPLIED','INTRODUCED','RETIRED','UNKNOWN')),
 observed_at INTEGER NOT NULL CHECK(typeof(observed_at)='integer' AND observed_at>=0), source TEXT NOT NULL,
 allocation TEXT NOT NULL CHECK(allocation IN ('AVAILABLE','QUARANTINED','RESERVED','ISSUED_TO_FF')),
 holder TEXT REFERENCES reservation_owners(id),
 CHECK((allocation IN ('AVAILABLE','QUARANTINED') AND holder IS NULL) OR (allocation IN ('RESERVED','ISSUED_TO_FF') AND holder IS NOT NULL)),
 CHECK(allocation NOT IN ('AVAILABLE','ISSUED_TO_FF') OR external_status='INTRODUCED'));
CREATE TABLE IF NOT EXISTS memberships(
 child TEXT PRIMARY KEY NOT NULL REFERENCES codes(id), parent TEXT NOT NULL REFERENCES codes(id),
 operation_id TEXT NOT NULL REFERENCES operations(id), CHECK(child<>parent));
CREATE TABLE IF NOT EXISTS operation_codes(
 operation_id TEXT NOT NULL REFERENCES operations(id), code_id TEXT NOT NULL,
 role TEXT NOT NULL CHECK(role IN ('DOCUMENT','PARENT','CHILD')), PRIMARY KEY(operation_id,code_id));
CREATE TABLE IF NOT EXISTS issues(
 id TEXT PRIMARY KEY NOT NULL CHECK(length(id)=32 AND id NOT GLOB '*[^0-9a-f]*'),
 state TEXT NOT NULL CHECK(state IN ('RESERVED','COMMITTED')), manifest TEXT,
 created_at INTEGER NOT NULL CHECK(typeof(created_at)='integer' AND created_at>=0),
 item_count INTEGER NOT NULL CHECK(typeof(item_count)='integer' AND item_count>0), committed_payload BLOB,
 CHECK((state='RESERVED' AND manifest IS NULL AND committed_payload IS NULL) OR
       (state='COMMITTED' AND manifest IS NOT NULL AND length(manifest)=64 AND manifest NOT GLOB '*[^0-9a-f]*' AND committed_payload IS NOT NULL AND json_valid(committed_payload))));
CREATE TABLE IF NOT EXISTS reservation_owners(
 id TEXT PRIMARY KEY NOT NULL, operation_id TEXT UNIQUE REFERENCES operations(id), issue_id TEXT UNIQUE REFERENCES issues(id),
 CHECK((operation_id IS NOT NULL AND issue_id IS NULL AND id=operation_id) OR
       (operation_id IS NULL AND issue_id IS NOT NULL AND id=issue_id)));
CREATE TABLE IF NOT EXISTS reservations(
 code_id TEXT PRIMARY KEY NOT NULL REFERENCES codes(id), owner_id TEXT NOT NULL REFERENCES reservation_owners(id));
CREATE TABLE IF NOT EXISTS issue_items(
 issue_id TEXT NOT NULL REFERENCES issues(id), code_id TEXT NOT NULL UNIQUE REFERENCES codes(id), PRIMARY KEY(issue_id,code_id));

CREATE TRIGGER IF NOT EXISTS immutable_metadata_update BEFORE UPDATE ON metadata BEGIN SELECT RAISE(ABORT,'immutable metadata'); END;
CREATE TRIGGER IF NOT EXISTS immutable_metadata_delete BEFORE DELETE ON metadata BEGIN SELECT RAISE(ABORT,'immutable metadata'); END;
CREATE TRIGGER IF NOT EXISTS immutable_operation BEFORE UPDATE OF id,fingerprint,payload_hash,environment,participant,kind,demand,payload,created_at ON operations BEGIN SELECT RAISE(ABORT,'immutable operation'); END;
CREATE TRIGGER IF NOT EXISTS immutable_operation_delete BEFORE DELETE ON operations BEGIN SELECT RAISE(ABORT,'immutable operation'); END;
CREATE TRIGGER IF NOT EXISTS immutable_approval BEFORE UPDATE OF id,operation_id,payload_hash,environment,participant,owner,created_at,expires_at,duplicate_review ON approvals BEGIN SELECT RAISE(ABORT,'immutable approval'); END;
CREATE TRIGGER IF NOT EXISTS approval_one_use BEFORE UPDATE OF consumed ON approvals WHEN OLD.consumed=1 BEGIN SELECT RAISE(ABORT,'approval consumed'); END;
CREATE TRIGGER IF NOT EXISTS immutable_approval_delete BEFORE DELETE ON approvals BEGIN SELECT RAISE(ABORT,'immutable approval'); END;
CREATE TRIGGER IF NOT EXISTS immutable_attempt BEFORE UPDATE OF id,operation_id,approval_id,submitted_at,business_hash,transport_hash,signed_hash,wire_hash,wire,baseline,environment,participant ON attempts BEGIN SELECT RAISE(ABORT,'immutable attempt'); END;
CREATE TRIGGER IF NOT EXISTS immutable_attempt_delete BEFORE DELETE ON attempts BEGIN SELECT RAISE(ABORT,'immutable attempt'); END;
CREATE TRIGGER IF NOT EXISTS attempt_start BEFORE INSERT ON attempts BEGIN
 SELECT CASE WHEN NEW.state<>'SUBMITTING' OR NOT EXISTS(SELECT 1 FROM approvals a JOIN operations o ON o.id=a.operation_id
 WHERE a.id=NEW.approval_id AND a.consumed=1 AND o.state='APPROVED' AND NEW.submitted_at>=a.created_at AND NEW.submitted_at<a.expires_at)
 THEN RAISE(ABORT,'attempt not authorized') END; END;
CREATE TRIGGER IF NOT EXISTS attempt_state BEFORE UPDATE OF state ON attempts BEGIN
 SELECT CASE WHEN NEW.state<>(SELECT state FROM operations WHERE id=NEW.operation_id) THEN RAISE(ABORT,'attempt state mismatch') END; END;
CREATE TRIGGER IF NOT EXISTS trusted_correlation BEFORE UPDATE OF external_id ON attempts BEGIN
 SELECT CASE WHEN OLD.external_id IS NOT NULL AND NEW.external_id IS NOT OLD.external_id THEN RAISE(ABORT,'immutable trusted correlation') END;
 SELECT CASE WHEN NEW.external_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM validated_evidence e WHERE e.attempt_id=NEW.id AND json_extract(e.body,'$.external_id')=NEW.external_id)
 THEN RAISE(ABORT,'unproven correlation') END; END;
CREATE TRIGGER IF NOT EXISTS immutable_evidence_update BEFORE UPDATE ON validated_evidence BEGIN SELECT RAISE(ABORT,'immutable evidence'); END;
CREATE TRIGGER IF NOT EXISTS immutable_evidence_delete BEFORE DELETE ON validated_evidence BEGIN SELECT RAISE(ABORT,'immutable evidence'); END;
CREATE TRIGGER IF NOT EXISTS evidence_binding BEFORE INSERT ON validated_evidence BEGIN
 SELECT CASE WHEN NOT EXISTS(SELECT 1 FROM attempts a JOIN operations o ON o.id=a.operation_id WHERE a.id=NEW.attempt_id AND o.id=NEW.operation_id
 AND a.rowid=(SELECT MAX(rowid) FROM attempts WHERE operation_id=o.id)
 AND json_extract(NEW.body,'$.operation_id')=o.id AND json_extract(NEW.body,'$.payload_hash')=o.payload_hash
 AND json_extract(NEW.body,'$.environment')=o.environment AND json_extract(NEW.body,'$.participant')=o.participant
 AND json_extract(NEW.body,'$.kind')=o.kind AND json_extract(NEW.body,'$.outcome')=NEW.outcome
 AND json_extract(NEW.body,'$.attempt_id')=a.id
 AND (a.external_id IS NULL OR a.external_id IS json_extract(NEW.body,'$.external_id'))
 AND ((NEW.phase='SUBMISSION' AND a.state='SUBMITTING' AND NEW.outcome IN ('ACCEPTED','REJECTED'))
 OR (NEW.phase='RECONCILIATION' AND a.state='RECONCILING' AND NEW.outcome IN ('SUCCEEDED','REJECTED','PARTIAL')))
 AND ((NEW.outcome='ACCEPTED' AND json_type(NEW.body,'$.complete')='false') OR
 (NEW.outcome<>'ACCEPTED' AND json_type(NEW.body,'$.complete')='true'))
 AND ((NEW.outcome='REJECTED' AND json_extract(NEW.body,'$.http_status') BETWEEN 400 AND 599) OR
 (NEW.outcome<>'REJECTED' AND json_extract(NEW.body,'$.http_status') BETWEEN 200 AND 299 AND json_type(NEW.body,'$.external_id')='text')))
 THEN RAISE(ABORT,'evidence binding mismatch') END; END;
CREATE TRIGGER IF NOT EXISTS operation_state BEFORE UPDATE OF state ON operations BEGIN
 SELECT CASE WHEN NOT ((OLD.state='PREPARED' AND NEW.state IN ('APPROVED','CANCELLED')) OR
 (OLD.state='APPROVED' AND NEW.state IN ('SUBMITTING','INVALIDATED','CANCELLED')) OR
 (OLD.state='SUBMITTING' AND NEW.state IN ('UNKNOWN','ACCEPTED','REJECTED')) OR
 (OLD.state IN ('UNKNOWN','ACCEPTED') AND NEW.state='RECONCILING') OR
 (OLD.state='RECONCILING' AND NEW.state IN ('UNKNOWN','SUCCEEDED','REJECTED','PARTIAL')) OR
 (OLD.state='REJECTED' AND NEW.state='APPROVED')) THEN RAISE(ABORT,'invalid state edge') END;
 SELECT CASE WHEN NEW.state='APPROVED' AND NOT EXISTS(SELECT 1 FROM approvals WHERE operation_id=NEW.id AND consumed=0) THEN RAISE(ABORT,'approval required') END;
 SELECT CASE WHEN NEW.state='SUBMITTING' AND NOT EXISTS(SELECT 1 FROM attempts WHERE operation_id=NEW.id AND state='SUBMITTING') THEN RAISE(ABORT,'attempt required') END;
 SELECT CASE WHEN NEW.state IN ('ACCEPTED','SUCCEEDED','REJECTED','PARTIAL') AND NOT EXISTS(
 SELECT 1 FROM validated_evidence e JOIN attempts a ON a.id=e.attempt_id WHERE e.operation_id=NEW.id AND e.outcome=NEW.state
 AND a.rowid=(SELECT MAX(rowid) FROM attempts WHERE operation_id=NEW.id)
 AND ((OLD.state='SUBMITTING' AND e.phase='SUBMISSION') OR (OLD.state='RECONCILING' AND e.phase='RECONCILIATION')))
 THEN RAISE(ABORT,'validated evidence required') END; END;

CREATE TRIGGER IF NOT EXISTS immutable_owner_update BEFORE UPDATE ON reservation_owners BEGIN SELECT RAISE(ABORT,'immutable owner'); END;
CREATE TRIGGER IF NOT EXISTS immutable_reservation_update BEFORE UPDATE ON reservations BEGIN SELECT RAISE(ABORT,'immutable reservation'); END;
CREATE TRIGGER IF NOT EXISTS acquire_reservation BEFORE INSERT ON reservations BEGIN
 SELECT CASE WHEN NOT EXISTS(SELECT 1 FROM codes c JOIN reservation_owners r ON r.id=NEW.owner_id WHERE c.id=NEW.code_id AND c.holder IS NULL
 AND c.allocation IN ('AVAILABLE','QUARANTINED') AND c.external_status NOT IN ('RETIRED','UNKNOWN')
 AND ((r.issue_id IS NOT NULL AND c.allocation='AVAILABLE' AND c.external_status='INTRODUCED' AND (SELECT state FROM issues WHERE id=r.issue_id)='RESERVED')
 OR (r.operation_id IS NOT NULL AND (SELECT state FROM operations WHERE id=r.operation_id)='APPROVED'
 AND EXISTS(SELECT 1 FROM operation_codes WHERE operation_id=r.operation_id AND code_id=c.id)
 AND EXISTS(SELECT 1 FROM operations o WHERE o.id=r.operation_id AND ((o.kind='SET' AND c.external_status='INTRODUCED') OR (o.kind='UTILISATION' AND c.external_status='EMITTED' AND c.gtin=json_extract(o.payload,'$.gtin')) OR (o.kind='INTRODUCTION' AND c.external_status='APPLIED' AND c.gtin=json_extract(o.payload,'$.gtin')))))))
 THEN RAISE(ABORT,'reservation unavailable') END;
 SELECT CASE WHEN EXISTS(SELECT 1 FROM memberships WHERE child=NEW.code_id OR parent=NEW.code_id) THEN RAISE(ABORT,'membership reserved') END; END;
CREATE TRIGGER IF NOT EXISTS reservation_to_code AFTER INSERT ON reservations BEGIN UPDATE codes SET allocation='RESERVED',holder=NEW.owner_id WHERE id=NEW.code_id; END;
CREATE TRIGGER IF NOT EXISTS release_reservation BEFORE DELETE ON reservations BEGIN
 SELECT CASE WHEN EXISTS(SELECT 1 FROM reservation_owners r JOIN issues i ON i.id=r.issue_id WHERE r.id=OLD.owner_id AND i.state='COMMITTED')
 OR EXISTS(SELECT 1 FROM memberships WHERE child=OLD.code_id OR parent=OLD.code_id) THEN RAISE(ABORT,'committed reservation') END;
 SELECT CASE WHEN EXISTS(SELECT 1 FROM reservation_owners r JOIN operations o ON o.id=r.operation_id WHERE r.id=OLD.owner_id
 AND NOT (o.state IN ('CANCELLED','INVALIDATED') OR EXISTS(
 SELECT 1 FROM validated_evidence e JOIN attempts a ON a.id=e.attempt_id WHERE e.operation_id=o.id
 AND a.rowid=(SELECT MAX(rowid) FROM attempts WHERE operation_id=o.id)
 AND ((o.state='REJECTED' AND e.outcome='REJECTED') OR (o.state='RECONCILING' AND e.phase='RECONCILIATION'
 AND (e.outcome='REJECTED' OR (e.outcome='SUCCEEDED' AND o.kind IN ('UTILISATION','INTRODUCTION'))))))))
 THEN RAISE(ABORT,'active operation reservation') END; END;
CREATE TRIGGER IF NOT EXISTS release_to_code AFTER DELETE ON reservations BEGIN UPDATE codes SET allocation='QUARANTINED',holder=NULL WHERE id=OLD.code_id; END;
CREATE TRIGGER IF NOT EXISTS code_owner_update BEFORE UPDATE OF allocation,holder ON codes BEGIN
 SELECT CASE WHEN NEW.holder IS NOT NULL AND NOT EXISTS(SELECT 1 FROM reservations WHERE code_id=NEW.id AND owner_id=NEW.holder) THEN RAISE(ABORT,'code owner mismatch') END;
 SELECT CASE WHEN NEW.holder IS NULL AND EXISTS(SELECT 1 FROM reservations WHERE code_id=NEW.id) THEN RAISE(ABORT,'reservation not released') END;
 SELECT CASE WHEN NEW.allocation='ISSUED_TO_FF' AND NOT EXISTS(SELECT 1 FROM reservation_owners r JOIN issues i ON i.id=r.issue_id WHERE r.id=NEW.holder AND i.state='COMMITTED') THEN RAISE(ABORT,'issue not committed') END;
 SELECT CASE WHEN OLD.allocation='ISSUED_TO_FF' AND (NEW.holder IS NOT OLD.holder OR NEW.allocation<>OLD.allocation) THEN RAISE(ABORT,'issued code immutable') END; END;
CREATE TRIGGER IF NOT EXISTS code_owner_insert BEFORE INSERT ON codes WHEN NEW.holder IS NOT NULL BEGIN SELECT RAISE(ABORT,'acquire reservation separately'); END;
CREATE TRIGGER IF NOT EXISTS immutable_binding_update BEFORE UPDATE ON operation_codes BEGIN SELECT RAISE(ABORT,'immutable binding'); END;
CREATE TRIGGER IF NOT EXISTS immutable_binding_delete BEFORE DELETE ON operation_codes BEGIN SELECT RAISE(ABORT,'immutable binding'); END;
CREATE TRIGGER IF NOT EXISTS membership_acquire BEFORE INSERT ON memberships BEGIN
 SELECT CASE WHEN NOT EXISTS(SELECT 1 FROM operations WHERE id=NEW.operation_id AND kind='SET' AND state='RECONCILING')
 OR NOT EXISTS(SELECT 1 FROM validated_evidence WHERE operation_id=NEW.operation_id AND outcome='SUCCEEDED' AND phase='RECONCILIATION')
 OR NOT EXISTS(SELECT 1 FROM reservations WHERE code_id=NEW.child AND owner_id=NEW.operation_id)
 OR NOT EXISTS(SELECT 1 FROM reservations WHERE code_id=NEW.parent AND owner_id=NEW.operation_id)
 OR NOT EXISTS(SELECT 1 FROM operation_codes WHERE operation_id=NEW.operation_id AND code_id=NEW.child AND role='CHILD')
 OR NOT EXISTS(SELECT 1 FROM operation_codes WHERE operation_id=NEW.operation_id AND code_id=NEW.parent AND role='PARENT')
 THEN RAISE(ABORT,'membership ownership mismatch') END; END;
CREATE TRIGGER IF NOT EXISTS immutable_membership_update BEFORE UPDATE ON memberships BEGIN SELECT RAISE(ABORT,'immutable membership'); END;
CREATE TRIGGER IF NOT EXISTS immutable_membership_delete BEFORE DELETE ON memberships BEGIN SELECT RAISE(ABORT,'immutable membership'); END;

CREATE TRIGGER IF NOT EXISTS issue_initial BEFORE INSERT ON issues WHEN NEW.state<>'RESERVED' BEGIN SELECT RAISE(ABORT,'issue must be reserved'); END;
CREATE TRIGGER IF NOT EXISTS immutable_issue BEFORE UPDATE ON issues WHEN OLD.state='COMMITTED' BEGIN SELECT RAISE(ABORT,'committed issue immutable'); END;
CREATE TRIGGER IF NOT EXISTS immutable_issue_delete BEFORE DELETE ON issues WHEN OLD.state='COMMITTED' BEGIN SELECT RAISE(ABORT,'committed issue immutable'); END;
CREATE TRIGGER IF NOT EXISTS issue_item_insert BEFORE INSERT ON issue_items BEGIN
 SELECT CASE WHEN (SELECT state FROM issues WHERE id=NEW.issue_id)<>'RESERVED' OR NOT EXISTS(SELECT 1 FROM reservations WHERE code_id=NEW.code_id AND owner_id=NEW.issue_id)
 THEN RAISE(ABORT,'issue item ownership mismatch') END; END;
CREATE TRIGGER IF NOT EXISTS immutable_issue_item_update BEFORE UPDATE ON issue_items BEGIN SELECT RAISE(ABORT,'immutable issue item'); END;
CREATE TRIGGER IF NOT EXISTS immutable_issue_item_delete BEFORE DELETE ON issue_items WHEN (SELECT state FROM issues WHERE id=OLD.issue_id)='COMMITTED' BEGIN SELECT RAISE(ABORT,'committed composition immutable'); END;
CREATE TRIGGER IF NOT EXISTS issue_commit BEFORE UPDATE OF state ON issues BEGIN
 SELECT CASE WHEN OLD.state<>'RESERVED' OR NEW.state<>'COMMITTED' OR NEW.id<>OLD.id OR NEW.item_count<>OLD.item_count OR NEW.created_at<>OLD.created_at
 OR (SELECT COUNT(*) FROM issue_items WHERE issue_id=NEW.id)<>NEW.item_count
 OR (SELECT COUNT(*) FROM issue_items x JOIN codes c ON c.id=x.code_id JOIN reservations r ON r.code_id=c.id
 WHERE x.issue_id=NEW.id AND c.holder=NEW.id AND r.owner_id=NEW.id AND c.allocation='RESERVED' AND c.external_status='INTRODUCED')<>NEW.item_count
 OR json_extract(NEW.committed_payload,'$.issue') IS NOT NEW.id OR json_type(NEW.committed_payload,'$.simulation') IS NOT 'true'
 OR json_extract(NEW.committed_payload,'$.environment') IS NOT (SELECT environment FROM metadata)
 OR json_array_length(NEW.committed_payload,'$.items') IS NOT NEW.item_count
 OR EXISTS(SELECT code_id FROM issue_items WHERE issue_id=NEW.id EXCEPT SELECT value FROM json_each(NEW.committed_payload,'$.items'))
 THEN RAISE(ABORT,'issue commit mismatch') END; END;
CREATE TRIGGER IF NOT EXISTS immutable_event_update BEFORE UPDATE ON events BEGIN SELECT RAISE(ABORT,'append-only events'); END;
CREATE TRIGGER IF NOT EXISTS immutable_event_delete BEFORE DELETE ON events BEGIN SELECT RAISE(ABORT,'append-only events'); END;
CREATE TRIGGER IF NOT EXISTS event_reference BEFORE INSERT ON events BEGIN
 SELECT CASE WHEN NEW.environment<>(SELECT environment FROM metadata)
 OR NOT (EXISTS(SELECT 1 FROM operations WHERE id=NEW.operation_id) OR EXISTS(SELECT 1 FROM issues WHERE id=NEW.operation_id))
 OR NEW.transition NOT IN ('PREPARED','QUEUE_RESERVED','QUEUE_COMMITTED','PREPARED->APPROVED','PREPARED->CANCELLED','APPROVED->SUBMITTING','APPROVED->INVALIDATED','APPROVED->CANCELLED','SUBMITTING->UNKNOWN','SUBMITTING->ACCEPTED','SUBMITTING->REJECTED','UNKNOWN->RECONCILING','ACCEPTED->RECONCILING','RECONCILING->UNKNOWN','RECONCILING->SUCCEEDED','RECONCILING->REJECTED','RECONCILING->PARTIAL','REJECTED->APPROVED')
 OR EXISTS(SELECT 1 FROM json_each(NEW.details) WHERE key NOT IN ('payload_hash','external_id','http_status','response_hash','outcome','count'))
 THEN RAISE(ABORT,'unsafe event') END; END;

-- Reject REPLACE as well as UPDATE/DELETE; initial rows cannot claim terminal authority.
CREATE TRIGGER IF NOT EXISTS operation_initial BEFORE INSERT ON operations WHEN NEW.state<>'PREPARED' OR EXISTS(SELECT 1 FROM operations WHERE id=NEW.id) BEGIN SELECT RAISE(ABORT,'operation must start prepared'); END;
CREATE TRIGGER IF NOT EXISTS approval_initial BEFORE INSERT ON approvals WHEN NEW.consumed<>0 OR EXISTS(SELECT 1 FROM approvals WHERE id=NEW.id) BEGIN SELECT RAISE(ABORT,'approval must be new'); END;
CREATE TRIGGER IF NOT EXISTS attempt_replace BEFORE INSERT ON attempts WHEN EXISTS(SELECT 1 FROM attempts WHERE id=NEW.id) BEGIN SELECT RAISE(ABORT,'immutable attempt'); END;
CREATE TRIGGER IF NOT EXISTS evidence_replace BEFORE INSERT ON validated_evidence WHEN EXISTS(SELECT 1 FROM validated_evidence WHERE id=NEW.id) BEGIN SELECT RAISE(ABORT,'immutable evidence'); END;
CREATE TRIGGER IF NOT EXISTS issue_replace BEFORE INSERT ON issues WHEN EXISTS(SELECT 1 FROM issues WHERE id=NEW.id) BEGIN SELECT RAISE(ABORT,'immutable issue'); END;
CREATE TRIGGER IF NOT EXISTS issue_item_replace BEFORE INSERT ON issue_items WHEN EXISTS(SELECT 1 FROM issue_items WHERE code_id=NEW.code_id) BEGIN SELECT RAISE(ABORT,'immutable issue item'); END;
CREATE TRIGGER IF NOT EXISTS membership_replace BEFORE INSERT ON memberships WHEN EXISTS(SELECT 1 FROM memberships WHERE child=NEW.child) BEGIN SELECT RAISE(ABORT,'immutable membership'); END;
CREATE TRIGGER IF NOT EXISTS binding_insert BEFORE INSERT ON operation_codes BEGIN
 SELECT CASE WHEN EXISTS(SELECT 1 FROM operation_codes WHERE operation_id=NEW.operation_id AND code_id=NEW.code_id)
 OR (SELECT state FROM operations WHERE id=NEW.operation_id)<>'PREPARED'
 THEN RAISE(ABORT,'immutable declared binding') END; END;
CREATE TRIGGER IF NOT EXISTS event_replace BEFORE INSERT ON events WHEN EXISTS(SELECT 1 FROM events WHERE seq=NEW.seq) BEGIN SELECT RAISE(ABORT,'append-only events'); END;
CREATE TRIGGER IF NOT EXISTS membership_composition BEFORE INSERT ON memberships BEGIN
 SELECT CASE WHEN NOT EXISTS(SELECT 1 FROM operations o,json_each(o.payload,'$.sets') u,json_each(u.value,'$.children') c
 WHERE o.id=NEW.operation_id AND json_extract(u.value,'$.parent')=NEW.parent AND c.value=NEW.child)
 THEN RAISE(ABORT,'membership composition mismatch') END; END;
CREATE TRIGGER IF NOT EXISTS event_details BEFORE INSERT ON events BEGIN
 SELECT CASE WHEN json_type(NEW.details)<>'object' OR EXISTS(SELECT 1 FROM json_each(NEW.details) WHERE
 CASE key
 WHEN 'payload_hash' THEN type<>'text' OR length(value)<>64 OR value GLOB '*[^0-9a-f]*'
 WHEN 'response_hash' THEN type<>'text' OR length(value)<>64 OR value GLOB '*[^0-9a-f]*'
 WHEN 'external_id' THEN type NOT IN ('text','null') OR (type='text' AND (value NOT GLOB 'SYN:external-[0-9]*' OR substr(value,14) GLOB '*[^0-9]*'))
 WHEN 'http_status' THEN type<>'integer' OR value NOT BETWEEN 100 AND 599
 WHEN 'count' THEN type<>'integer' OR value<0
 WHEN 'outcome' THEN type<>'text' OR value NOT IN ('ACCEPTED','SUCCEEDED','REJECTED','PARTIAL','UNCERTAIN','RESTART','INSUFFICIENT')
 ELSE 1 END) THEN RAISE(ABORT,'unsafe event details') END; END;
