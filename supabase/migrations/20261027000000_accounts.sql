-- Bee's Dashboard accounts (Supabase). Run once in the Supabase SQL editor (or `supabase db push`).
--
-- Security model: the website only ever holds the project's public (anon / publishable) key. Every table below has
-- Row Level Security switched on and policies that let a signed-in user see and change ONLY their own rows; the
-- anon role gets nothing. No service-role key is used anywhere in this project.

-- ---------------------------------------------------------------- profiles: one row per user
create table if not exists public.profiles (
  id           uuid primary key references auth.users (id) on delete cascade,
  display_name text check (display_name is null or char_length(display_name) between 1 and 60),
  settings     jsonb not null default '{}'::jsonb check (jsonb_typeof(settings) = 'object' and pg_column_size(settings) < 16384),
  created_at   timestamptz not null default now(),
  updated_at   timestamptz not null default now()
);

-- ---------------------------------------------------------------- watchlist: the coins a user follows, with a note
create table if not exists public.watchlist (
  user_id    uuid not null default auth.uid() references auth.users (id) on delete cascade,
  symbol     text not null check (symbol ~ '^[A-Z0-9]{1,20}USDT$'),
  note       text check (note is null or char_length(note) <= 500),
  position   integer not null default 0,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  primary key (user_id, symbol)
);

-- ---------------------------------------------------------------- saved views: named filters / layouts (for later)
create table if not exists public.saved_views (
  id         uuid primary key default gen_random_uuid(),
  user_id    uuid not null default auth.uid() references auth.users (id) on delete cascade,
  name       text not null check (char_length(name) between 1 and 60),
  route      text not null check (route ~ '^[a-z0-9/_-]{1,80}$'),
  state      jsonb not null default '{}'::jsonb check (jsonb_typeof(state) = 'object' and pg_column_size(state) < 16384),
  created_at timestamptz not null default now()
);
create index if not exists saved_views_user on public.saved_views (user_id);

-- ---------------------------------------------------------------- Row Level Security: own rows only
alter table public.profiles    enable row level security;
alter table public.watchlist   enable row level security;
alter table public.saved_views enable row level security;

revoke all on public.profiles, public.watchlist, public.saved_views from anon;
grant select, insert, update, delete on public.profiles, public.watchlist, public.saved_views to authenticated;

drop policy if exists "own profile" on public.profiles;
create policy "own profile" on public.profiles for all to authenticated
  using ((select auth.uid()) = id) with check ((select auth.uid()) = id);

drop policy if exists "own watchlist" on public.watchlist;
create policy "own watchlist" on public.watchlist for all to authenticated
  using ((select auth.uid()) = user_id) with check ((select auth.uid()) = user_id);

drop policy if exists "own views" on public.saved_views;
create policy "own views" on public.saved_views for all to authenticated
  using ((select auth.uid()) = user_id) with check ((select auth.uid()) = user_id);

-- ---------------------------------------------------------------- limits: no table can be used as free storage
create or replace function public.enforce_row_limits() returns trigger
language plpgsql security definer set search_path = '' as $$
begin
  if tg_table_name = 'watchlist' and (select count(*) from public.watchlist where user_id = new.user_id) >= 300 then
    raise exception 'watchlist limit reached (300 coins)';
  end if;
  if tg_table_name = 'saved_views' and (select count(*) from public.saved_views where user_id = new.user_id) >= 50 then
    raise exception 'saved views limit reached (50)';
  end if;
  return new;
end $$;

drop trigger if exists watchlist_limit on public.watchlist;
create trigger watchlist_limit before insert on public.watchlist for each row execute function public.enforce_row_limits();
drop trigger if exists views_limit on public.saved_views;
create trigger views_limit before insert on public.saved_views for each row execute function public.enforce_row_limits();

-- ---------------------------------------------------------------- updated_at
create or replace function public.touch_updated_at() returns trigger
language plpgsql set search_path = '' as $$
begin
  new.updated_at := now();
  return new;
end $$;

drop trigger if exists profiles_touch on public.profiles;
create trigger profiles_touch before update on public.profiles for each row execute function public.touch_updated_at();
drop trigger if exists watchlist_touch on public.watchlist;
create trigger watchlist_touch before update on public.watchlist for each row execute function public.touch_updated_at();

-- ---------------------------------------------------------------- a profile for every new user (email or Google)
create or replace function public.handle_new_user() returns trigger
language plpgsql security definer set search_path = '' as $$
begin
  insert into public.profiles (id, display_name)
  values (new.id, left(coalesce(nullif(new.raw_user_meta_data ->> 'display_name', ''),
                                nullif(new.raw_user_meta_data ->> 'full_name', ''),
                                nullif(new.raw_user_meta_data ->> 'name', ''),
                                split_part(new.email, '@', 1)), 60))
  on conflict (id) do nothing;
  return new;
end $$;

drop trigger if exists on_auth_user_created on auth.users;
create trigger on_auth_user_created after insert on auth.users for each row execute function public.handle_new_user();

-- ---------------------------------------------------------------- a user can delete their own account (and data)
create or replace function public.delete_my_account() returns void
language plpgsql security definer set search_path = '' as $$
begin
  if auth.uid() is null then
    raise exception 'not signed in';
  end if;
  delete from auth.users where id = auth.uid();   -- profiles, watchlist and saved_views cascade
end $$;

revoke all on function public.delete_my_account() from public, anon;
grant execute on function public.delete_my_account() to authenticated;
revoke all on function public.enforce_row_limits() from public, anon, authenticated;
revoke all on function public.handle_new_user() from public, anon, authenticated;
