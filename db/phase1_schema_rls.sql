-- Phase 1: Core schema + role hierarchy + backend access control (PostgreSQL)
-- Designed for DB-first enforcement (RLS + triggers), no UI assumptions.

begin;

-- -----------------------------------------------------------------------------
-- Enums
-- -----------------------------------------------------------------------------
create type public.user_role as enum ('salesperson', 'manager', 'regional_head', 'admin');
create type public.client_type as enum ('New', 'Existing');
create type public.quarter_type as enum ('Q1', 'Q2', 'Q3', 'Q4');

-- -----------------------------------------------------------------------------
-- Core users + hierarchy
-- -----------------------------------------------------------------------------
create table public.app_users (
  id bigserial primary key,
  auth_user_id uuid not null unique,
  full_name text not null,
  email text not null unique,
  role public.user_role not null,
  region text,

  -- optional direct supervisors (kept consistent with trigger)
  manager_user_id bigint references public.app_users(id),
  regional_head_user_id bigint references public.app_users(id),

  -- permissions toggles
  can_delete_own_entries boolean not null default false,
  can_edit_team_entries boolean not null default false,
  can_delete_team_entries boolean not null default false,
  can_edit_region_entries boolean not null default false,
  can_delete_region_entries boolean not null default false,

  is_active boolean not null default true,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),

  constraint manager_required_for_salesperson
    check (role <> 'salesperson' or manager_user_id is not null),
  constraint regional_required_for_non_admin
    check (role = 'admin' or regional_head_user_id is not null)
);

create index idx_app_users_role on public.app_users(role);
create index idx_app_users_region on public.app_users(region);
create index idx_app_users_manager on public.app_users(manager_user_id);
create index idx_app_users_regional on public.app_users(regional_head_user_id);

-- Flexible closure table for hierarchy traversal and rollups.
-- Depth: 0=self, 1=direct report, 2+=indirect report.
create table public.user_hierarchy (
  ancestor_user_id bigint not null references public.app_users(id) on delete cascade,
  descendant_user_id bigint not null references public.app_users(id) on delete cascade,
  depth int not null check (depth >= 0),
  primary key (ancestor_user_id, descendant_user_id)
);

create index idx_user_hierarchy_descendant on public.user_hierarchy(descendant_user_id, ancestor_user_id);

-- -----------------------------------------------------------------------------
-- Revenue entries
-- -----------------------------------------------------------------------------
create table public.revenue_entries (
  id bigserial primary key,

  client_id text,
  client_name text not null,
  client_type public.client_type not null,
  agency_name text,
  campaign_name text,

  user_id bigint not null references public.app_users(id),             -- assigned salesperson/owner
  manager_id bigint references public.app_users(id),                   -- auto-derived
  regional_head_id bigint references public.app_users(id),             -- auto-derived
  region text,

  quarter public.quarter_type not null,
  entry_date date not null,

  plan_shared boolean not null default false,
  plan_date date,
  plan_value numeric(14,2) not null default 0,

  negotiation_stage text,
  pipeline_value numeric(14,2) not null default 0,

  ro_date date,
  ro_value numeric(14,2) not null default 0,

  status text,
  follow_up_date date,
  remarks text,

  created_by bigint not null references public.app_users(id),
  updated_by bigint not null references public.app_users(id),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create index idx_revenue_entries_user_id on public.revenue_entries(user_id);
create index idx_revenue_entries_manager_id on public.revenue_entries(manager_id);
create index idx_revenue_entries_regional_head_id on public.revenue_entries(regional_head_id);
create index idx_revenue_entries_region on public.revenue_entries(region);
create index idx_revenue_entries_quarter on public.revenue_entries(quarter);
create index idx_revenue_entries_client_type on public.revenue_entries(client_type);
create index idx_revenue_entries_entry_date on public.revenue_entries(entry_date);
create index idx_revenue_entries_follow_up_date on public.revenue_entries(follow_up_date);

-- Optional: per-user quarterly targets for rollups.
create table public.sales_targets (
  id bigserial primary key,
  user_id bigint not null references public.app_users(id) on delete cascade,
  quarter public.quarter_type not null,
  fiscal_year int not null,
  target_value numeric(14,2) not null check (target_value >= 0),
  created_by bigint not null references public.app_users(id),
  updated_by bigint not null references public.app_users(id),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (user_id, quarter, fiscal_year)
);

create index idx_sales_targets_user_qtr on public.sales_targets(user_id, fiscal_year, quarter);

-- -----------------------------------------------------------------------------
-- Helper functions (auth + hierarchy)
-- -----------------------------------------------------------------------------
-- Supports both Supabase-style auth.uid() and plain Postgres apps via SET app.user_id.
create or replace function public.current_app_user_id()
returns bigint
language plpgsql
stable
as $$
declare
  v_auth_uuid uuid;
  v_user_id bigint;
begin
  begin
    v_auth_uuid := auth.uid();
  exception when undefined_function then
    v_auth_uuid := null;
  end;

  if v_auth_uuid is not null then
    select u.id into v_user_id
    from public.app_users u
    where u.auth_user_id = v_auth_uuid
      and u.is_active = true;

    return v_user_id;
  end if;

  begin
    return nullif(current_setting('app.user_id', true), '')::bigint;
  exception when others then
    return null;
  end;
end;
$$;

create or replace function public.current_role()
returns public.user_role
language sql
stable
as $$
  select u.role
  from public.app_users u
  where u.id = public.current_app_user_id();
$$;

create or replace function public.is_descendant(ancestor_id bigint, descendant_id bigint)
returns boolean
language sql
stable
as $$
  select exists (
    select 1
    from public.user_hierarchy h
    where h.ancestor_user_id = ancestor_id
      and h.descendant_user_id = descendant_id
  );
$$;

create or replace function public.can_manage_entry(entry_owner_id bigint)
returns boolean
language plpgsql
stable
as $$
declare
  actor_id bigint := public.current_app_user_id();
  actor_role public.user_role;
  actor_region text;
  owner_region text;
  allow_team_edit boolean;
  allow_region_edit boolean;
begin
  if actor_id is null then
    return false;
  end if;

  select role, region, can_edit_team_entries, can_edit_region_entries
    into actor_role, actor_region, allow_team_edit, allow_region_edit
  from public.app_users
  where id = actor_id;

  if actor_role = 'admin' then
    return true;
  end if;

  if actor_role = 'manager' then
    return allow_team_edit and public.is_descendant(actor_id, entry_owner_id);
  end if;

  if actor_role = 'regional_head' then
    select u.region into owner_region from public.app_users u where u.id = entry_owner_id;
    return allow_region_edit and owner_region = actor_region;
  end if;

  return false;
end;
$$;

create or replace function public.can_delete_entry(entry_owner_id bigint)
returns boolean
language plpgsql
stable
as $$
declare
  actor_id bigint := public.current_app_user_id();
  actor_role public.user_role;
  actor_region text;
  owner_region text;
  own_delete boolean;
  team_delete boolean;
  region_delete boolean;
begin
  if actor_id is null then
    return false;
  end if;

  select role, region, can_delete_own_entries, can_delete_team_entries, can_delete_region_entries
    into actor_role, actor_region, own_delete, team_delete, region_delete
  from public.app_users
  where id = actor_id;

  if actor_role = 'admin' then
    return true;
  end if;

  if actor_role = 'salesperson' then
    return actor_id = entry_owner_id and own_delete;
  end if;

  if actor_role = 'manager' then
    return team_delete and public.is_descendant(actor_id, entry_owner_id);
  end if;

  if actor_role = 'regional_head' then
    select u.region into owner_region from public.app_users u where u.id = entry_owner_id;
    return region_delete and owner_region = actor_region;
  end if;

  return false;
end;
$$;

-- -----------------------------------------------------------------------------
-- Trigger functions for integrity
-- -----------------------------------------------------------------------------
create or replace function public.tg_set_updated_at()
returns trigger
language plpgsql
as $$
begin
  new.updated_at := now();
  return new;
end;
$$;

-- Keep hierarchy references in sync for revenue rows.
create or replace function public.tg_fill_revenue_hierarchy()
returns trigger
language plpgsql
as $$
declare
  owner_manager_id bigint;
  owner_regional_id bigint;
  owner_region text;
  actor_id bigint := public.current_app_user_id();
  actor_role public.user_role;
begin
  select role into actor_role from public.app_users where id = actor_id;

  -- Salesperson can only create/update own rows.
  if actor_role = 'salesperson' and new.user_id <> actor_id then
    raise exception 'Salesperson can only assign entries to self';
  end if;

  -- Manager can create/update only for descendants.
  if actor_role = 'manager' and not public.is_descendant(actor_id, new.user_id) then
    raise exception 'Manager can only assign entries to team members';
  end if;

  -- Regional head can assign only within region.
  if actor_role = 'regional_head' then
    if not exists (
      select 1
      from public.app_users u
      where u.id = new.user_id
        and u.region = (select region from public.app_users where id = actor_id)
    ) then
      raise exception 'Regional head can only assign entries within their region';
    end if;
  end if;

  select manager_user_id, regional_head_user_id, region
    into owner_manager_id, owner_regional_id, owner_region
  from public.app_users
  where id = new.user_id;

  new.manager_id := owner_manager_id;
  new.regional_head_id := owner_regional_id;
  new.region := owner_region;

  if tg_op = 'INSERT' then
    new.created_by := actor_id;
  end if;
  new.updated_by := actor_id;

  return new;
end;
$$;

-- -----------------------------------------------------------------------------
-- Apply triggers
-- -----------------------------------------------------------------------------
create trigger trg_app_users_updated_at
before update on public.app_users
for each row
execute function public.tg_set_updated_at();

create trigger trg_revenue_entries_updated_at
before update on public.revenue_entries
for each row
execute function public.tg_set_updated_at();

create trigger trg_sales_targets_updated_at
before update on public.sales_targets
for each row
execute function public.tg_set_updated_at();

create trigger trg_revenue_entries_fill_hierarchy
before insert or update on public.revenue_entries
for each row
execute function public.tg_fill_revenue_hierarchy();

-- -----------------------------------------------------------------------------
-- RLS enablement
-- -----------------------------------------------------------------------------
alter table public.app_users enable row level security;
alter table public.user_hierarchy enable row level security;
alter table public.revenue_entries enable row level security;
alter table public.sales_targets enable row level security;

-- app_users visibility rules
create policy app_users_select_policy on public.app_users
for select
using (
  -- Admin sees all
  public.current_role() = 'admin'
  or
  -- Regional head sees users in own region
  (
    public.current_role() = 'regional_head'
    and region = (select u.region from public.app_users u where u.id = public.current_app_user_id())
  )
  or
  -- Manager sees team users + self
  (
    public.current_role() = 'manager'
    and (
      id = public.current_app_user_id()
      or public.is_descendant(public.current_app_user_id(), id)
    )
  )
  or
  -- Salesperson sees only self
  (
    public.current_role() = 'salesperson'
    and id = public.current_app_user_id()
  )
);

-- Only admin can mutate user/hierarchy metadata.
create policy app_users_admin_write on public.app_users
for all
using (public.current_role() = 'admin')
with check (public.current_role() = 'admin');

create policy hierarchy_select_policy on public.user_hierarchy
for select
using (
  public.current_role() = 'admin'
  or ancestor_user_id = public.current_app_user_id()
  or descendant_user_id = public.current_app_user_id()
);

create policy hierarchy_admin_write on public.user_hierarchy
for all
using (public.current_role() = 'admin')
with check (public.current_role() = 'admin');

-- revenue_entries read access
create policy revenue_entries_select_policy on public.revenue_entries
for select
using (
  -- salesperson: own rows
  (public.current_role() = 'salesperson' and user_id = public.current_app_user_id())
  or
  -- manager: all descendants
  (public.current_role() = 'manager' and public.is_descendant(public.current_app_user_id(), user_id))
  or
  -- regional: same region
  (
    public.current_role() = 'regional_head'
    and region = (select u.region from public.app_users u where u.id = public.current_app_user_id())
  )
  or
  -- admin: all
  public.current_role() = 'admin'
);

-- create rules
create policy revenue_entries_insert_policy on public.revenue_entries
for insert
with check (
  -- salesperson can add own
  (public.current_role() = 'salesperson' and user_id = public.current_app_user_id())
  or
  -- manager can add for descendants
  (public.current_role() = 'manager' and public.is_descendant(public.current_app_user_id(), user_id))
  or
  -- regional can add for own region users
  (
    public.current_role() = 'regional_head'
    and exists (
      select 1 from public.app_users u
      where u.id = revenue_entries.user_id
        and u.region = (select a.region from public.app_users a where a.id = public.current_app_user_id())
    )
  )
  or
  -- admin unrestricted
  public.current_role() = 'admin'
);

-- update rules
create policy revenue_entries_update_policy on public.revenue_entries
for update
using (
  user_id = public.current_app_user_id()  -- own rows
  or public.can_manage_entry(user_id)     -- manager/regional/admin
)
with check (
  user_id = public.current_app_user_id()
  or public.can_manage_entry(user_id)
);

-- delete rules
create policy revenue_entries_delete_policy on public.revenue_entries
for delete
using (public.can_delete_entry(user_id));

-- sales_targets: readable by same visibility logic as users.
create policy sales_targets_select_policy on public.sales_targets
for select
using (
  public.current_role() = 'admin'
  or (public.current_role() = 'salesperson' and user_id = public.current_app_user_id())
  or (public.current_role() = 'manager' and public.is_descendant(public.current_app_user_id(), user_id))
  or (
    public.current_role() = 'regional_head'
    and exists (
      select 1 from public.app_users au
      where au.id = sales_targets.user_id
        and au.region = (select me.region from public.app_users me where me.id = public.current_app_user_id())
    )
  )
);

create policy sales_targets_write_policy on public.sales_targets
for all
using (
  public.current_role() = 'admin'
  or public.can_manage_entry(user_id)
)
with check (
  public.current_role() = 'admin'
  or public.can_manage_entry(user_id)
);

commit;
