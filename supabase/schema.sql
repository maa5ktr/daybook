create extension if not exists pgcrypto with schema extensions;

create table if not exists public.profiles (
  id uuid primary key references auth.users (id) on delete cascade,
  email text not null,
  display_name text not null,
  is_owner boolean not null default false,
  created_at timestamptz not null default now()
);

create table if not exists public.invites (
  id uuid primary key default gen_random_uuid(),
  code_hash text not null unique,
  created_by uuid not null references public.profiles (id) on delete cascade,
  expires_at timestamptz not null,
  used_at timestamptz,
  used_by uuid references auth.users (id) on delete set null,
  created_at timestamptz not null default now()
);

create table if not exists public.owner_setup_codes (
  singleton boolean primary key default true check (singleton),
  code_hash text not null,
  expires_at timestamptz not null,
  used_at timestamptz
);

create table if not exists public.transactions (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users (id) on delete cascade,
  transaction_type text not null check (transaction_type in ('expense', 'income')),
  amount_cents bigint not null check (amount_cents > 0),
  category text not null,
  transaction_date date not null,
  note text not null default '' check (char_length(note) <= 80),
  legacy_id text,
  created_at timestamptz not null default now(),
  constraint transaction_category_matches_type check (
    (transaction_type = 'expense' and category in ('Food', 'Transport', 'Utilities', 'Fun', 'Other'))
    or (transaction_type = 'income' and category in ('Salary', 'Freelance', 'Business', 'Gift', 'Other'))
  )
);

create unique index if not exists transactions_legacy_id_unique
  on public.transactions (user_id, legacy_id);
create index if not exists transactions_user_date_idx
  on public.transactions (user_id, transaction_date desc, created_at desc);

create or replace function public.handle_new_daybook_user()
returns trigger
language plpgsql
security definer
set search_path = ''
as $$
declare
  invite_code text;
  owner_setup_code text;
  is_first_user boolean;
  display_name text;
begin
  perform pg_advisory_xact_lock(72194321);
  select not exists (select 1 from public.profiles) into is_first_user;
  display_name := coalesce(
    nullif(new.raw_user_meta_data ->> 'display_name', ''),
    split_part(new.email, '@', 1)
  );

  if is_first_user then
    owner_setup_code := nullif(new.raw_user_meta_data ->> 'owner_setup_code', '');
    update public.owner_setup_codes
    set used_at = now()
    where singleton
      and code_hash = encode(extensions.digest(owner_setup_code, 'sha256'), 'hex')
      and used_at is null
      and expires_at > now();

    if not found then
      raise exception 'A valid first-time setup code is required.';
    end if;
  else
    invite_code := nullif(new.raw_user_meta_data ->> 'invite_code', '');
    if invite_code is null then
      raise exception 'A valid Daybook invitation is required.';
    end if;

    update public.invites
    set used_at = now(), used_by = new.id
    where code_hash = encode(extensions.digest(invite_code, 'sha256'), 'hex')
      and used_at is null
      and expires_at > now();

    if not found then
      raise exception 'This Daybook invitation is invalid, expired, or already used.';
    end if;
  end if;

  insert into public.profiles (id, email, display_name, is_owner)
  values (new.id, new.email, display_name, is_first_user);
  return new;
end;
$$;

create or replace function public.create_daybook_owner_setup_code()
returns text
language plpgsql
security definer
set search_path = ''
as $$
declare
  setup_code text := encode(extensions.gen_random_bytes(18), 'hex');
begin
  if exists (select 1 from public.profiles) then
    raise exception 'An owner setup code can only be created before the first account.';
  end if;

  insert into public.owner_setup_codes (singleton, code_hash, expires_at, used_at)
  values (true, encode(extensions.digest(setup_code, 'sha256'), 'hex'), now() + interval '24 hours', null)
  on conflict (singleton) do update
  set code_hash = excluded.code_hash,
      expires_at = excluded.expires_at,
      used_at = null;

  return setup_code;
end;
$$;

drop trigger if exists on_auth_user_created_daybook on auth.users;
create trigger on_auth_user_created_daybook
  after insert on auth.users
  for each row execute function public.handle_new_daybook_user();

create or replace function public.create_daybook_invite()
returns text
language plpgsql
security definer
set search_path = ''
as $$
declare
  invite_code text := encode(extensions.gen_random_bytes(18), 'hex');
begin
  if not exists (
    select 1 from public.profiles
    where id = (select auth.uid()) and is_owner
  ) then
    raise exception 'Only the Daybook owner can create invitations.';
  end if;

  insert into public.invites (code_hash, created_by, expires_at)
  values (
    encode(extensions.digest(invite_code, 'sha256'), 'hex'),
    (select auth.uid()),
    now() + interval '7 days'
  );
  return invite_code;
end;
$$;

alter table public.profiles enable row level security;
alter table public.invites enable row level security;
alter table public.owner_setup_codes enable row level security;
alter table public.transactions enable row level security;

revoke all on table public.profiles, public.invites, public.owner_setup_codes, public.transactions from anon, authenticated;
grant select on table public.profiles to authenticated;
grant select, insert, delete on table public.transactions to authenticated;
grant execute on function public.create_daybook_invite() to authenticated;
revoke all on function public.handle_new_daybook_user() from public, anon, authenticated;
revoke all on function public.create_daybook_invite() from public, anon;
revoke all on function public.create_daybook_owner_setup_code() from public, anon, authenticated;

drop policy if exists "Users read their own profile" on public.profiles;
create policy "Users read their own profile"
  on public.profiles for select to authenticated
  using ((select auth.uid()) = id);

drop policy if exists "Users manage their own transactions" on public.transactions;
create policy "Users manage their own transactions"
  on public.transactions for all to authenticated
  using ((select auth.uid()) = user_id)
  with check ((select auth.uid()) = user_id);