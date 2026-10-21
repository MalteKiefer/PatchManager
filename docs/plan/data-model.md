# Data model

Draft for all phases. Each phase refines its part before implementation. All primary
keys are UUIDv7. Every table except `tenant`, the identity tables and instance settings
carries `tenant_id` and an RLS policy.

```mermaid
erDiagram
    TENANT ||--o{ SITE : owns
    SITE ||--o{ BUILDING : has
    BUILDING ||--o{ FLOOR : has
    FLOOR ||--o{ ROOM : has
    FLOOR ||--o| FLOOR_PLAN : "drawn on"
    ROOM ||--o{ RACK : contains
    ROOM ||--o{ DEVICE : "floor device"
    RACK ||--o{ RACK_SPACE : "U ranges"
    DEVICE ||--o{ RACK_SPACE : occupies
    VENDOR ||--o{ DEVICE_TYPE : makes
    DEVICE_TYPE ||--o{ PORT_TEMPLATE : defines
    DEVICE_TYPE ||--o{ DEVICE : instantiates
    DEVICE ||--o{ PORT : has
    PORT ||--o| PORT : "front maps to rear"
    CABLE ||--|{ CABLE_END : "A and B"
    PORT ||--o| CABLE_END : "terminates"
    TENANT ||--o{ VLAN : defines
    PORT ||--o{ PORT_VLAN : carries
    VLAN ||--o{ PORT_VLAN : "on"
    WLAN }o--|| VLAN : bridges
    WLAN ||--o{ WLAN_BROADCAST : "served by"
    PORT ||--o{ WLAN_BROADCAST : "radio"
    USER }o--o{ GROUP : "member of"
    GROUP }o--o{ GROUP : "nested in"
    ROLE ||--o{ ASSIGNMENT : granted
    USER ||--o{ ASSIGNMENT : principal
    GROUP ||--o{ ASSIGNMENT : principal
    AUDIT_EVENT }o--o| USER : actor

    TENANT {
        uuid id PK
        text slug UK
        text name
        jsonb address
        timestamptz archived_at
    }
    SITE {
        uuid id PK
        uuid tenant_id FK
        text name
        jsonb address
        point geo
        ltree path
    }
    BUILDING {
        uuid id PK
        uuid site_id FK
        text name
        ltree path
    }
    FLOOR {
        uuid id PK
        uuid building_id FK
        text name
        int level
        ltree path
    }
    ROOM {
        uuid id PK
        uuid floor_id FK
        text name
        jsonb outline
        ltree path
    }
    FLOOR_PLAN {
        uuid id PK
        uuid floor_id FK
        text file_key
        numeric px_per_metre
    }
    RACK {
        uuid id PK
        uuid room_id FK
        text name
        int u_height
        int width_mm
        int depth_mm
        bool descending_units
        numeric plan_x
        numeric plan_y
        ltree path
    }
    RACK_OCCUPANCY {
        uuid rack_id FK
        uuid device_id FK
        text face
        int4range units
    }
    VENDOR {
        uuid id PK
        uuid tenant_id "null = shared"
        text name
    }
    DEVICE_TYPE {
        uuid id PK
        uuid vendor_id FK
        text model
        text form "rack|floor"
        text category
        int u_height
        bool full_depth
        text front_image
        text rear_image
    }
    PORT_TEMPLATE {
        uuid id PK
        uuid device_type_id FK
        text name_pattern
        text side "front|rear|interface|wireless"
        text connector
        int speed_mbps
        int count
    }
    DEVICE {
        uuid id PK
        uuid tenant_id FK
        uuid device_type_id FK
        uuid room_id FK
        uuid rack_id FK
        text name
        text serial
        text asset_tag
        text status
        numeric plan_x
        numeric plan_y
        ltree path
    }
    PORT {
        uuid id PK
        uuid device_id FK
        text name
        text side
        text connector
        int speed_mbps
        uuid rear_port_id FK
        int rear_position
    }
    CABLE {
        uuid id PK
        uuid tenant_id FK
        text kind "permanent|patch"
        text media
        text colour
        numeric length_m
        text label
    }
    CABLE_END {
        uuid cable_id FK
        char end "A|B"
        uuid port_id FK "unique"
    }
    VLAN {
        uuid id PK
        uuid tenant_id FK
        uuid site_id "null = tenant wide"
        int vid "1..4094"
        text name
    }
    PORT_VLAN {
        uuid port_id FK
        uuid vlan_id FK
        text mode "access|tagged"
    }
    WLAN {
        uuid id PK
        uuid tenant_id FK
        text ssid
        uuid vlan_id FK
        text security
    }
    USER {
        uuid id PK
        citext email UK
        text password "argon2id"
        bool is_active
    }
    ROLE {
        uuid id PK
        text name
        bool is_system
        text_array permissions
    }
    ASSIGNMENT {
        uuid id PK
        uuid role_id FK
        uuid user_id
        uuid group_id
        ltree scope
        jsonb conditions
        text effect "allow|deny"
    }
    AUDIT_EVENT {
        bigint seq PK
        timestamptz at
        uuid actor_id
        uuid tenant_id
        text action
        text object_type
        uuid object_id
        jsonb before
        jsonb after
        bytea prev_hash
        bytea hash
    }
```

## Database enforced invariants

| Rule | Mechanism |
|---|---|
| No two things share a U on the same rack face | `EXCLUDE USING gist (rack_id WITH =, faces WITH &&, units WITH &&)` on `dcim_rackspace`; `faces` is `[0,1)` front, `[1,2)` rear, `[0,2)` both |
| Space fits in the rack | trigger `pm_check_rackspace`; trigger `pm_check_rack_height` stops shrinking below used units |
| Child in parent's tenant, path consistent | trigger `pm_check_hierarchy` on every tree table |
| A port has at most one cable | unique `cable_end.port_id` |
| A cable has exactly ends A and B | unique `(cable_id, end)`, deferred check for count = 2 |
| VLAN id range and uniqueness | `CHECK (vid BETWEEN 1 AND 4094)`, unique `(tenant_id, coalesce(site_id), vid)` |
| Names unique within parent | unique `(parent_id, lower(name))` |
| Cross tenant references impossible | composite foreign keys `(tenant_id, parent_id)` referencing `(tenant_id, id)` |
| Audit log append only | `REVOKE UPDATE, DELETE`; trigger rejects both |

## Search

Each searchable table gets a generated `tsvector` column (`simple` config, so names
and serials are not stemmed) with a GIN index, plus `gin_trgm_ops` indexes on name,
serial and asset tag for fuzzy matching. Global search unions per type selectors, so
results are already permission and tenant filtered.

## Generic attachments

Photos, notes and tags attach to any object through `(object_type, object_id)` plus
`tenant_id`. Files are stored by content hash; metadata rows hold MIME type, size and
the sanitised derivative key.
