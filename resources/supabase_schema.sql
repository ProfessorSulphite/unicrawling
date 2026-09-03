-- Supabase schema for the education-counselling corpus (C29).
--
-- Designed against the post-C17/C18 payload, and shaped by what C19 established:
-- almost every field is genuinely nullable, because the pipeline stopped
-- inventing values to fill gaps. A NOT NULL here would either reject good rows
-- or push someone back toward fabricating a value to satisfy the column.
--
-- Only four things are required, and each is required for a reason:
--   universities.domain      the natural key everything is filed under
--   universities.name        a university nobody can name is not a row
--   programs.name            what a student searches for
--   programs.degree_level    which of the four buckets it belongs to
--
-- Those four are exactly CRITICAL_PROGRAM_FIELDS plus the university identity,
-- so the audit gate in inspector/auditor.py and this schema agree on what
-- cannot be missing.

create table if not exists universities (
    -- Canonical domain, matching resources/rankings_global.json's key and the
    -- slug the pipeline files everything under. Natural rather than surrogate:
    -- a re-extraction of the same university must update its row, not add one.
    domain                       text primary key,
    slug                         text not null,
    name                         text not null,
    abbreviation                 text,
    country                      text,
    city                         text,
    website                      text,
    type                         text check (type in ('public', 'private', 'other')),
    established_year             integer,
    accreditation_body           text,
    primary_instruction_language text,
    admission_cycles_offered     text[]  not null default '{}',
    description                  text,
    academics_url                text,
    admissions_url               text,
    application_portal_url       text,
    -- Provenance, so a consumer can tell a sourced fact from an extracted one.
    domain_verified              boolean not null default false,
    verification_note            text,
    exa_enriched                 boolean not null default false,
    programs_possibly_truncated  boolean not null default false,
    extracted_at                 timestamptz not null default now(),
    updated_at                   timestamptz not null default now()
);

create table if not exists programs (
    id                       bigint generated always as identity primary key,
    university_domain        text not null references universities (domain) on delete cascade,

    name                     text not null,
    -- The four canonical levels (C17). Enforced here as well as in Pydantic:
    -- a row that reaches the database through any other path still cannot
    -- invent a fifth level.
    degree_level             text not null
                             check (degree_level in ('bachelors', 'masters', 'phd', 'diploma')),
    department               text,
    duration                 text,

    -- Money is stored exactly as the university published it, with a separate
    -- currency label and no conversion (plan section 5, Finding 8). Text, not
    -- numeric: "PKR 150,000 per semester" is the published fact, and parsing it
    -- into a number would silently drop the "per semester".
    tuition_fee              text,
    currency                 text,
    application_fee          text,

    admission_requirements   text,
    eligibility_min_marks    text,
    eligibility_entry_tests  text[] not null default '{}',
    eligibility_formula      text,

    application_deadlines    text[] not null default '{}',
    application_status       text check (application_status in ('open', 'closed', 'rolling', 'upcoming')),
    intake_terms             text[] not null default '{}',
    delivery_mode            text,

    description              text,
    career_prospects         text,
    scholarships_info        text,
    courses_taught           text[] not null default '{}',
    program_info_link        text,

    updated_at               timestamptz not null default now(),

    -- One row per (university, programme, level). A re-extraction upserts onto
    -- this rather than appending a second copy of the same degree.
    unique (university_domain, name, degree_level)
);

create table if not exists faculties (
    id                bigint generated always as identity primary key,
    university_domain text not null references universities (domain) on delete cascade,
    faculty_name      text not null,
    departments       text[] not null default '{}',
    unique (university_domain, faculty_name)
);

create table if not exists contacts (
    university_domain text primary key references universities (domain) on delete cascade,
    official_email    text,
    phone_numbers     text[] not null default '{}',
    physical_address  text
);

create table if not exists rankings (
    id                bigint generated always as identity primary key,
    university_domain text not null references universities (domain) on delete cascade,
    source            text not null,
    scope             text,
    subject           text,
    year              integer not null,
    rank              integer not null,
    source_url        text,
    -- Rankings come only from the sourced registry, never from the model, so a
    -- duplicate (source, year, subject) for one university is a bug upstream.
    unique (university_domain, source, year, subject)
);

create index if not exists programs_level_idx    on programs (degree_level);
create index if not exists programs_country_idx  on programs (university_domain);
create index if not exists universities_country  on universities (country);

-- Read-only to the public; writes come from the pipeline's service role only.
alter table universities enable row level security;
alter table programs     enable row level security;
alter table faculties    enable row level security;
alter table contacts     enable row level security;
alter table rankings     enable row level security;
