# -*- coding: utf-8 -*-
"""
디스코드 올인원 봇 (관리 + 자판기/포인트 시스템)  -  bot.py 단일 파일

설치:   pip install -U "discord.py>=2.3"
실행:   환경변수 DISCORD_TOKEN 설정 후  python bot.py
        (또는 아래 TOKEN 에 직접 입력)

필수 설정 (디스코드 개발자 포털 > Bot):
  - SERVER MEMBERS INTENT  ON  (환영/퇴장 메시지에 필요)

입금 자동 확인(선택):
  환경변수 WEBHOOK_PORT=8080, WEBHOOK_SECRET=아무_긴_비밀문자열 설정 시
  POST http://서버주소:8080/deposit  (헤더 X-Secret: 비밀문자열)
  본문 {"name": "홍길동", "amount": 10000}  을 받으면 자동으로 포인트가 지급됩니다.
  (은행 입금 알림을 이 주소로 보내주는 외부 서비스/프로그램이 따로 필요합니다)
"""
import os
import sqlite3

import discord
from discord import app_commands
from discord.ext import commands

TOKEN = os.getenv("DISCORD_TOKEN", "여기에_봇_토큰_입력")
DB_PATH = "bot.db"
WEBHOOK_PORT = int(os.getenv("WEBHOOK_PORT", "0"))
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")
MIN_CHARGE = 1000

# ───────────────────────── DB ─────────────────────────
db = sqlite3.connect(DB_PATH)
db.row_factory = sqlite3.Row
db.executescript("""
CREATE TABLE IF NOT EXISTS settings(
    guild_id INTEGER PRIMARY KEY,
    welcome_channel INTEGER, welcome_msg TEXT,
    leave_channel INTEGER, leave_msg TEXT,
    ticket_title TEXT, ticket_desc TEXT, notice TEXT,
    bank TEXT, account TEXT, holder TEXT,
    order_channel INTEGER, order_role INTEGER, charge_channel INTEGER
);
CREATE TABLE IF NOT EXISTS points(
    guild_id INTEGER, user_id INTEGER, balance INTEGER DEFAULT 0,
    PRIMARY KEY(guild_id, user_id)
);
CREATE TABLE IF NOT EXISTS products(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER, name TEXT, stock INTEGER, price INTEGER
);
CREATE TABLE IF NOT EXISTS verify(
    message_id INTEGER PRIMARY KEY,
    guild_id INTEGER, channel_id INTEGER, role_id INTEGER, emoji TEXT
);
CREATE TABLE IF NOT EXISTS charges(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER, user_id INTEGER, amount INTEGER, depositor TEXT,
    status TEXT DEFAULT 'pending'
);
CREATE TABLE IF NOT EXISTS orders(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER, user_id INTEGER, product TEXT, qty INTEGER, total INTEGER,
    status TEXT DEFAULT 'pending'
);
""")
db.commit()

SETTING_COLS = {
    "welcome_channel", "welcome_msg", "leave_channel", "leave_msg",
    "ticket_title", "ticket_desc", "notice", "bank", "account", "holder",
    "order_channel", "order_role", "charge_channel",
}


def run(sql, args=()):
    cur = db.execute(sql, args)
    db.commit()
    return cur


def get(sql, args=()):
    return db.execute(sql, args).fetchone()


def rows(sql, args=()):
    return db.execute(sql, args).fetchall()


def get_setting(gid, key, default=None):
    row = get("SELECT * FROM settings WHERE guild_id=?", (gid,))
    if row is not None and row[key] is not None:
        return row[key]
    return default


def set_setting(gid, **kw):
    run("INSERT OR IGNORE INTO settings(guild_id) VALUES(?)", (gid,))
    for k, v in kw.items():
        assert k in SETTING_COLS
        run(f"UPDATE settings SET {k}=? WHERE guild_id=?", (v, gid))


def get_points(gid, uid):
    row = get("SELECT balance FROM points WHERE guild_id=? AND user_id=?", (gid, uid))
    return row["balance"] if row else 0


def add_points(gid, uid, amount):
    """양수 지급 / 음수 차감 (0 아래로는 내려가지 않음). 변경 후 잔액 반환."""
    run("INSERT OR IGNORE INTO points(guild_id,user_id,balance) VALUES(?,?,0)", (gid, uid))
    run("UPDATE points SET balance=MAX(0, balance+?) WHERE guild_id=? AND user_id=?",
        (amount, gid, uid))
    return get_points(gid, uid)


def purchase(gid, uid, pid, qty):
    p = get("SELECT * FROM products WHERE id=? AND guild_id=?", (pid, gid))
    if not p:
        return ("none",)
    if p["stock"] < qty:
        return ("stock", p)
    total = p["price"] * qty
    cur = db.execute(
        "UPDATE points SET balance=balance-? WHERE guild_id=? AND user_id=? AND balance>=?",
        (total, gid, uid, total))
    if cur.rowcount == 0:
        db.rollback()
        return ("balance", p, total)
    db.execute("UPDATE products SET stock=stock-? WHERE id=?", (qty, pid))
    cur = db.execute(
        "INSERT INTO orders(guild_id,user_id,product,qty,total) VALUES(?,?,?,?,?)",
        (gid, uid, p["name"], qty, total))
    db.commit()
    return ("ok", cur.lastrowid, p, total)


def confirm_charge(cid):
    """대기 중인 충전을 확정하고 포인트 지급. 성공 시 행 반환, 이미 처리됐으면 None."""
    cur = db.execute("UPDATE charges SET status='done' WHERE id=? AND status='pending'", (cid,))
    if cur.rowcount == 0:
        db.rollback()
        return None
    db.commit()
    row = get("SELECT * FROM charges WHERE id=?", (cid,))
    add_points(row["guild_id"], row["user_id"], row["amount"])
    return row


def is_admin(member: discord.Member):
    p = member.guild_permissions
    if p.administrator or p.manage_guild:
        return True
    rid = get_setting(member.guild.id, "order_role")
    return bool(rid and any(r.id == rid for r in member.roles))


def parse_int(text):
    try:
        return int(text.replace(",", "").replace("원", "").strip())
    except ValueError:
        return None


def norm_emoji(s):
    return str(s).replace("\ufe0f", "").strip()


def fmt(text, member):
    return (text.replace("{user}", member.mention)
                .replace("{name}", member.display_name)
                .replace("{server}", member.guild.name)
                .replace("{count}", str(member.guild.member_count)))


# ───────────────────────── 봇 ─────────────────────────
intents = discord.Intents.default()
intents.members = True


class Bot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=intents, help_command=None)

    async def setup_hook(self):
        self.add_view(TicketView())
        self.add_view(OrderView())
        self.add_view(ChargeView())
        if WEBHOOK_PORT:
            await start_webhook(self)

    async def sync_guild(self, guild):
        # 코드에 있는 명령어를 서버별로 즉시 동기화 (재시작/초대 시 자동 반영)
        self.tree.copy_global_to(guild=guild)
        await self.tree.sync(guild=guild)

    async def on_ready(self):
        for g in self.guilds:
            try:
                await self.sync_guild(g)
            except Exception as e:
                print(f"[sync 실패] {g.name}: {e}")
        print(f"로그인: {self.user}  / 서버 {len(self.guilds)}개 명령어 동기화 완료")

    async def on_guild_join(self, guild):
        await self.sync_guild(guild)


bot = Bot()
tree = bot.tree


@tree.error
async def on_tree_error(interaction: discord.Interaction, error):
    if isinstance(error, app_commands.MissingPermissions):
        msg = "❌ 관리자 권한이 필요합니다."
    else:
        msg = f"❌ 오류가 발생했습니다: {error}"
    if interaction.response.is_done():
        await interaction.followup.send(msg, ephemeral=True)
    else:
        await interaction.response.send_message(msg, ephemeral=True)


admin = app_commands.checks.has_permissions(administrator=True)

# ───────────────────── 환영 / 퇴장 ─────────────────────


@bot.event
async def on_member_join(member):
    cid = get_setting(member.guild.id, "welcome_channel")
    msg = get_setting(member.guild.id, "welcome_msg")
    ch = member.guild.get_channel(cid) if cid else None
    if ch and msg:
        await ch.send(fmt(msg, member))


@bot.event
async def on_member_remove(member):
    cid = get_setting(member.guild.id, "leave_channel")
    msg = get_setting(member.guild.id, "leave_msg")
    ch = member.guild.get_channel(cid) if cid else None
    if ch and msg:
        await ch.send(fmt(msg, member))


HELP_VARS = "사용 가능: {user}=멘션 {name}=닉네임 {server}=서버명 {count}=인원수"


@tree.command(name="환영설정", description="환영 메시지를 설정합니다")
@app_commands.describe(채널="환영 메시지를 보낼 채널", 메시지=HELP_VARS)
@app_commands.default_permissions(administrator=True)
@app_commands.guild_only()
@admin
async def welcome_set(i: discord.Interaction, 채널: discord.TextChannel,
                      메시지: str = "{user}님, {server}에 오신 것을 환영합니다! 🎉"):
    set_setting(i.guild_id, welcome_channel=채널.id, welcome_msg=메시지)
    await i.response.send_message(f"✅ 환영 메시지 설정 완료\n채널: {채널.mention}\n내용: {메시지}", ephemeral=True)


@tree.command(name="환영해제", description="환영 메시지를 끕니다")
@app_commands.default_permissions(administrator=True)
@app_commands.guild_only()
@admin
async def welcome_off(i: discord.Interaction):
    set_setting(i.guild_id, welcome_channel=None, welcome_msg=None)
    await i.response.send_message("✅ 환영 메시지를 껐습니다.", ephemeral=True)


@tree.command(name="퇴장설정", description="퇴장 메시지를 설정합니다")
@app_commands.describe(채널="퇴장 메시지를 보낼 채널", 메시지=HELP_VARS)
@app_commands.default_permissions(administrator=True)
@app_commands.guild_only()
@admin
async def leave_set(i: discord.Interaction, 채널: discord.TextChannel,
                    메시지: str = "{name}님이 서버를 떠났습니다. 👋"):
    set_setting(i.guild_id, leave_channel=채널.id, leave_msg=메시지)
    await i.response.send_message(f"✅ 퇴장 메시지 설정 완료\n채널: {채널.mention}\n내용: {메시지}", ephemeral=True)


@tree.command(name="퇴장해제", description="퇴장 메시지를 끕니다")
@app_commands.default_permissions(administrator=True)
@app_commands.guild_only()
@admin
async def leave_off(i: discord.Interaction):
    set_setting(i.guild_id, leave_channel=None, leave_msg=None)
    await i.response.send_message("✅ 퇴장 메시지를 껐습니다.", ephemeral=True)


# ───────────────────── 이모지 인증 역할 ─────────────────────


@tree.command(name="인증설정", description="이모지를 누르면 역할을 받는 인증 메시지를 만듭니다")
@app_commands.describe(채널="인증 메시지를 보낼 채널", 역할="지급할 역할", 이모지="반응 이모지 (예: ✅)",
                       제목="임베드 제목", 설명="임베드 설명")
@app_commands.default_permissions(administrator=True)
@app_commands.guild_only()
@admin
async def verify_set(i: discord.Interaction, 채널: discord.TextChannel, 역할: discord.Role, 이모지: str = "✅",
                     제목: str = "✅ 인증", 설명: str = "아래 이모지를 눌러 인증하세요!"):
    await i.response.defer(ephemeral=True)
    if 역할 >= i.guild.me.top_role:
        return await i.followup.send("❌ 봇의 역할이 해당 역할보다 낮습니다. 봇 역할을 위로 올려주세요.", ephemeral=True)
    try:
        msg = await 채널.send(embed=discord.Embed(title=제목, description=설명, color=0x57F287))
        await msg.add_reaction(이모지)
    except discord.HTTPException as e:
        return await i.followup.send(f"❌ 실패: {e}", ephemeral=True)
    run("INSERT OR REPLACE INTO verify VALUES(?,?,?,?,?)",
        (msg.id, i.guild_id, 채널.id, 역할.id, norm_emoji(이모지)))
    await i.followup.send(f"✅ 인증 메시지 생성 완료 ({채널.mention}, 역할 {역할.mention})", ephemeral=True)


@bot.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
    if not payload.guild_id or payload.user_id == bot.user.id:
        return
    row = get("SELECT * FROM verify WHERE message_id=?", (payload.message_id,))
    if not row or norm_emoji(payload.emoji) != row["emoji"]:
        return
    guild = bot.get_guild(payload.guild_id)
    member = payload.member or guild.get_member(payload.user_id)
    role = guild.get_role(row["role_id"])
    if member and role:
        try:
            await member.add_roles(role, reason="이모지 인증")
        except discord.HTTPException:
            pass


# ───────────────────── 청소 / 킥 / 밴 ─────────────────────


@tree.command(name="청소", description="메시지를 삭제합니다 (1~100)")
@app_commands.describe(개수="삭제할 메시지 수")
@app_commands.default_permissions(manage_messages=True)
@app_commands.guild_only()
@app_commands.checks.has_permissions(manage_messages=True)
async def clear(i: discord.Interaction, 개수: app_commands.Range[int, 1, 100]):
    await i.response.defer(ephemeral=True)
    deleted = await i.channel.purge(limit=개수)
    await i.followup.send(f"🧹 {len(deleted)}개의 메시지를 삭제했습니다.", ephemeral=True)


@tree.command(name="킥", description="멤버를 추방합니다")
@app_commands.describe(대상="추방할 멤버", 사유="사유")
@app_commands.default_permissions(kick_members=True)
@app_commands.guild_only()
@app_commands.checks.has_permissions(kick_members=True)
async def kick(i: discord.Interaction, 대상: discord.Member, 사유: str = "사유 없음"):
    if 대상.top_role >= i.guild.me.top_role or 대상 == i.guild.owner:
        return await i.response.send_message("❌ 해당 멤버를 추방할 수 없습니다 (역할 순위 확인).", ephemeral=True)
    await 대상.kick(reason=f"{i.user} : {사유}")
    await i.response.send_message(f"👢 {대상.mention} 추방 완료 (사유: {사유})")


@tree.command(name="밴", description="멤버를 차단합니다")
@app_commands.describe(대상="차단할 멤버", 사유="사유")
@app_commands.default_permissions(ban_members=True)
@app_commands.guild_only()
@app_commands.checks.has_permissions(ban_members=True)
async def ban(i: discord.Interaction, 대상: discord.Member, 사유: str = "사유 없음"):
    if 대상.top_role >= i.guild.me.top_role or 대상 == i.guild.owner:
        return await i.response.send_message("❌ 해당 멤버를 차단할 수 없습니다 (역할 순위 확인).", ephemeral=True)
    await 대상.ban(reason=f"{i.user} : {사유}")
    await i.response.send_message(f"🔨 {대상.mention} 차단 완료 (사유: {사유})")


# ───────────────────── 관리자 설정 (포인트/계좌/재고/공지) ─────────────────────


@tree.command(name="포인트", description="포인트 지급/차감 (양수=지급, 음수=차감)")
@app_commands.describe(대상="대상 유저", 금액="양수는 지급, 음수는 차감")
@app_commands.default_permissions(administrator=True)
@app_commands.guild_only()
@admin
async def points_cmd(i: discord.Interaction, 대상: discord.Member, 금액: int):
    new = add_points(i.guild_id, 대상.id, 금액)
    word = "지급" if 금액 >= 0 else "차감"
    await i.response.send_message(f"✅ {대상.mention} 에게 {abs(금액):,}P {word}\n현재 잔액: **{new:,}P**", ephemeral=True)


@tree.command(name="계좌설정", description="충전용 계좌를 설정합니다")
@app_commands.describe(은행="은행명", 계좌번호="계좌번호", 입금주="입금주명")
@app_commands.default_permissions(administrator=True)
@app_commands.guild_only()
@admin
async def account_set(i: discord.Interaction, 은행: str, 계좌번호: str, 입금주: str):
    set_setting(i.guild_id, bank=은행, account=계좌번호, holder=입금주)
    await i.response.send_message(f"✅ 계좌 설정 완료\n{은행} / {계좌번호} / {입금주}", ephemeral=True)


@tree.command(name="관리설정", description="주문 알림 채널, 관리자 역할, 충전 요청 채널 설정")
@app_commands.describe(주문채널="구매 알림이 올라올 채널", 관리자역할="주문 시 멘션할 관리자 역할(없으면 서버 주인 멘션)",
                       충전채널="충전 요청 승인 카드가 올라올 채널(없으면 주문채널)")
@app_commands.default_permissions(administrator=True)
@app_commands.guild_only()
@admin
async def manage_set(i: discord.Interaction, 주문채널: discord.TextChannel,
                     관리자역할: discord.Role = None, 충전채널: discord.TextChannel = None):
    set_setting(i.guild_id, order_channel=주문채널.id,
                order_role=관리자역할.id if 관리자역할 else None,
                charge_channel=(충전채널 or 주문채널).id)
    await i.response.send_message("✅ 관리 설정 완료", ephemeral=True)


class TicketModal(discord.ui.Modal, title="티켓 설명 설정"):
    t = discord.ui.TextInput(label="제목", max_length=100)
    d = discord.ui.TextInput(label="설명", style=discord.TextStyle.paragraph, max_length=1500)

    def __init__(self, gid):
        super().__init__()
        self.gid = gid
        self.t.default = get_setting(gid, "ticket_title", "🛒 자판기")
        self.d.default = get_setting(gid, "ticket_desc", "아래 버튼을 눌러 이용해주세요.")

    async def on_submit(self, i: discord.Interaction):
        set_setting(self.gid, ticket_title=self.t.value, ticket_desc=self.d.value)
        await i.response.send_message("✅ 티켓 설명이 저장되었습니다. /티켓전송 으로 새로 보내주세요.", ephemeral=True)


class NoticeModal(discord.ui.Modal, title="공지 설정"):
    n = discord.ui.TextInput(label="공지 내용", style=discord.TextStyle.paragraph, max_length=1500)

    def __init__(self, gid):
        super().__init__()
        self.gid = gid
        self.n.default = get_setting(gid, "notice", "")

    async def on_submit(self, i: discord.Interaction):
        set_setting(self.gid, notice=self.n.value)
        await i.response.send_message("✅ 공지가 저장되었습니다.", ephemeral=True)


@tree.command(name="티켓설명", description="티켓의 제목/설명을 커스텀합니다")
@app_commands.default_permissions(administrator=True)
@app_commands.guild_only()
@admin
async def ticket_desc(i: discord.Interaction):
    await i.response.send_modal(TicketModal(i.guild_id))


@tree.command(name="공지설정", description="[공지사항] 버튼에 보여줄 공지를 설정합니다")
@app_commands.default_permissions(administrator=True)
@app_commands.guild_only()
@admin
async def notice_set(i: discord.Interaction):
    await i.response.send_modal(NoticeModal(i.guild_id))


@tree.command(name="티켓전송", description="선택한 채널에 자판기 티켓을 보냅니다")
@app_commands.describe(채널="티켓을 보낼 채널")
@app_commands.default_permissions(administrator=True)
@app_commands.guild_only()
@admin
async def ticket_send(i: discord.Interaction, 채널: discord.TextChannel):
    embed = discord.Embed(
        title=get_setting(i.guild_id, "ticket_title", "🛒 자판기"),
        description=get_setting(i.guild_id, "ticket_desc", "아래 버튼을 눌러 이용해주세요."),
        color=0x5865F2)
    try:
        await 채널.send(embed=embed, view=TicketView())
    except discord.HTTPException as e:
        return await i.response.send_message(f"❌ 전송 실패: {e}", ephemeral=True)
    await i.response.send_message(f"✅ {채널.mention} 에 티켓을 보냈습니다.", ephemeral=True)


async def product_autocomplete(i: discord.Interaction, current: str):
    items = rows("SELECT name FROM products WHERE guild_id=? AND name LIKE ? LIMIT 25",
                 (i.guild_id, f"%{current}%"))
    return [app_commands.Choice(name=r["name"], value=r["name"]) for r in items]


@tree.command(name="재고추가", description="상품 재고를 추가합니다 (같은 이름이면 재고가 늘고 가격이 갱신됨)")
@app_commands.describe(상품명="상품 이름", 재고수="추가할 재고 수", 가격="개당 가격(포인트)")
@app_commands.default_permissions(administrator=True)
@app_commands.guild_only()
@app_commands.autocomplete(상품명=product_autocomplete)
@admin
async def stock_add(i: discord.Interaction, 상품명: str, 재고수: app_commands.Range[int, 1, 1000000],
                    가격: app_commands.Range[int, 0, 100000000]):
    row = get("SELECT * FROM products WHERE guild_id=? AND name=?", (i.guild_id, 상품명))
    if row:
        run("UPDATE products SET stock=stock+?, price=? WHERE id=?", (재고수, 가격, row["id"]))
        stock = row["stock"] + 재고수
    else:
        run("INSERT INTO products(guild_id,name,stock,price) VALUES(?,?,?,?)", (i.guild_id, 상품명, 재고수, 가격))
        stock = 재고수
    await i.response.send_message(f"✅ **{상품명}** / 재고 {stock}개 / {가격:,}P", ephemeral=True)


@tree.command(name="재고삭제", description="상품을 삭제합니다")
@app_commands.default_permissions(administrator=True)
@app_commands.guild_only()
@app_commands.autocomplete(상품명=product_autocomplete)
@admin
async def stock_del(i: discord.Interaction, 상품명: str):
    cur = run("DELETE FROM products WHERE guild_id=? AND name=?", (i.guild_id, 상품명))
    await i.response.send_message("✅ 삭제 완료" if cur.rowcount else "❌ 해당 상품이 없습니다.", ephemeral=True)


@tree.command(name="재고목록", description="등록된 상품 목록을 봅니다")
@app_commands.default_permissions(administrator=True)
@app_commands.guild_only()
@admin
async def stock_list(i: discord.Interaction):
    items = rows("SELECT * FROM products WHERE guild_id=?", (i.guild_id,))
    text = "\n".join(f"• **{p['name']}** — {p['price']:,}P / 재고 {p['stock']}개" for p in items) or "등록된 상품이 없습니다."
    await i.response.send_message(text, ephemeral=True)


# ───────────────────── 티켓 버튼 ─────────────────────


class QtyModal(discord.ui.Modal):
    qty = discord.ui.TextInput(label="구매 수량", placeholder="예: 1", max_length=6)

    def __init__(self, pid, name):
        super().__init__(title=f"구매: {name}"[:45])
        self.pid = pid

    async def on_submit(self, i: discord.Interaction):
        qty = parse_int(self.qty.value)
        if not qty or qty < 1:
            return await i.response.send_message("❌ 수량을 올바르게 입력해주세요.", ephemeral=True)
        gid = i.guild_id
        if not get_setting(gid, "order_channel"):
            return await i.response.send_message("❌ 아직 주문 채널이 설정되지 않았습니다. 관리자에게 문의해주세요.", ephemeral=True)
        res = purchase(gid, i.user.id, self.pid, qty)
        if res[0] == "none":
            return await i.response.send_message("❌ 존재하지 않는 상품입니다.", ephemeral=True)
        if res[0] == "stock":
            return await i.response.send_message(f"❌ 재고가 부족합니다. (남은 재고 {res[1]['stock']}개)", ephemeral=True)
        if res[0] == "balance":
            have = get_points(gid, i.user.id)
            return await i.response.send_message(
                f"💸 잔액이 부족합니다.\n필요: **{res[2]:,}P** / 보유: **{have:,}P**\n"
                f"티켓의 **[💳 잔액 충전]** 버튼으로 먼저 충전해주세요.", ephemeral=True)

        _, oid, p, total = res
        left = get_points(gid, i.user.id)
        await i.response.send_message(
            f"✅ 구매 완료!\n**{p['name']}** × {qty} = **{total:,}P**\n남은 잔액: **{left:,}P**\n"
            f"관리자가 확인 후 DM으로 상품을 지급해드립니다.", ephemeral=True)

        # 관리자 알림
        ch = i.guild.get_channel(get_setting(gid, "order_channel"))
        role_id = get_setting(gid, "order_role")
        mention = f"<@&{role_id}>" if role_id else (i.guild.owner.mention if i.guild.owner else "")
        embed = discord.Embed(title="🛒 새 주문", color=0xFEE75C)
        embed.add_field(name="구매자", value=f"{i.user.mention} ({i.user.display_name})", inline=False)
        embed.add_field(name="상품", value=p["name"])
        embed.add_field(name="수량", value=str(qty))
        embed.add_field(name="결제 금액", value=f"{total:,}P")
        embed.set_footer(text=f"주문번호 #{oid}")
        if ch:
            await ch.send(f"{mention} 새 주문이 들어왔습니다! 구매자에게 DM으로 상품을 지급해주세요.",
                          embed=embed, view=OrderView(),
                          allowed_mentions=discord.AllowedMentions(roles=True, users=True))


class ProductSelectView(discord.ui.View):
    def __init__(self, products):
        super().__init__(timeout=180)
        options = [discord.SelectOption(label=p["name"][:100], value=str(p["id"]),
                                        description=f"{p['price']:,}P · 재고 {p['stock']}개")
                   for p in products[:25]]
        sel = discord.ui.Select(placeholder="구매할 상품을 선택하세요", options=options)
        sel.callback = self.on_select
        self.add_item(sel)

    async def on_select(self, i: discord.Interaction):
        pid = int(i.data["values"][0])
        p = get("SELECT * FROM products WHERE id=?", (pid,))
        if not p or p["stock"] < 1:
            return await i.response.send_message("❌ 품절된 상품입니다.", ephemeral=True)
        await i.response.send_modal(QtyModal(pid, p["name"]))


class ChargeModal(discord.ui.Modal, title="💳 잔액 충전"):
    amount = discord.ui.TextInput(label=f"충전 금액 (최소 {MIN_CHARGE:,}원)", placeholder="예: 10000", max_length=10)
    depositor = discord.ui.TextInput(label="입금자명", placeholder="실제 입금하실 이름", max_length=20)

    async def on_submit(self, i: discord.Interaction):
        gid = i.guild_id
        amount = parse_int(self.amount.value)
        if amount is None or amount < MIN_CHARGE:
            return await i.response.send_message(f"❌ 충전 금액은 최소 {MIN_CHARGE:,}원 이상 숫자로 입력해주세요.", ephemeral=True)
        bank, acc, holder = (get_setting(gid, k) for k in ("bank", "account", "holder"))
        if not (bank and acc and holder):
            return await i.response.send_message("❌ 아직 계좌가 설정되지 않았습니다. 관리자에게 문의해주세요.", ephemeral=True)
        name = self.depositor.value.strip()
        cur = run("INSERT INTO charges(guild_id,user_id,amount,depositor) VALUES(?,?,?,?)",
                  (gid, i.user.id, amount, name))
        cid = cur.lastrowid
        embed = discord.Embed(title="💳 입금 안내", color=0x57F287,
                              description="아래 계좌로 **정확한 금액**을 **입금자명 그대로** 입금해주세요.\n입금이 확인되면 포인트가 지급됩니다.")
        embed.add_field(name="은행", value=bank)
        embed.add_field(name="계좌번호", value=acc)
        embed.add_field(name="예금주", value=holder)
        embed.add_field(name="입금 금액", value=f"{amount:,}원")
        embed.add_field(name="입금자명", value=name)
        await i.response.send_message(embed=embed, ephemeral=True)

        # 관리자 수동 승인 카드 (자동 확인이 안 될 때 대비)
        chid = get_setting(gid, "charge_channel") or get_setting(gid, "order_channel")
        ch = i.guild.get_channel(chid) if chid else None
        if ch:
            e = discord.Embed(title="💳 충전 요청", color=0x5865F2)
            e.add_field(name="유저", value=i.user.mention)
            e.add_field(name="금액", value=f"{amount:,}원")
            e.add_field(name="입금자명", value=name)
            e.set_footer(text=f"충전번호 #{cid}")
            await ch.send(embed=e, view=ChargeView())


class TicketView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="🛒 상품 구매", style=discord.ButtonStyle.primary, custom_id="t_buy")
    async def buy(self, i: discord.Interaction, b: discord.ui.Button):
        items = rows("SELECT * FROM products WHERE guild_id=? AND stock>0 ORDER BY id", (i.guild_id,))
        if not items:
            return await i.response.send_message("📦 현재 판매 중인 상품이 없습니다.", ephemeral=True)
        text = "\n".join(f"• **{p['name']}** — {p['price']:,}P (재고 {p['stock']}개)" for p in items[:25])
        embed = discord.Embed(title="🛒 상품 목록", description=text, color=0x5865F2)
        embed.set_footer(text=f"내 잔액: {get_points(i.guild_id, i.user.id):,}P")
        await i.response.send_message(embed=embed, view=ProductSelectView(items), ephemeral=True)

    @discord.ui.button(label="💳 잔액 충전", style=discord.ButtonStyle.success, custom_id="t_charge")
    async def charge(self, i: discord.Interaction, b: discord.ui.Button):
        await i.response.send_modal(ChargeModal())

    @discord.ui.button(label="📦 내 정보", style=discord.ButtonStyle.secondary, custom_id="t_info")
    async def info(self, i: discord.Interaction, b: discord.ui.Button):
        embed = discord.Embed(title="📦 내 정보", color=0xEB459E)
        embed.add_field(name="닉네임", value=i.user.display_name)
        embed.add_field(name="포인트 잔액", value=f"{get_points(i.guild_id, i.user.id):,}P")
        await i.response.send_message(embed=embed, ephemeral=True)

    @discord.ui.button(label="📢 공지사항", style=discord.ButtonStyle.secondary, custom_id="t_notice")
    async def notice(self, i: discord.Interaction, b: discord.ui.Button):
        text = get_setting(i.guild_id, "notice") or "등록된 공지가 없습니다."
        await i.response.send_message(embed=discord.Embed(title="📢 공지사항", description=text, color=0xED4245),
                                      ephemeral=True)


# ───────────────────── 주문 / 충전 승인 버튼 ─────────────────────


def footer_id(message: discord.Message):
    return int(message.embeds[0].footer.text.split("#")[1])


def disabled_view(view_cls):
    v = view_cls()
    for c in v.children:
        c.disabled = True
    return v


class OrderView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="✅ 지급 완료", style=discord.ButtonStyle.success, custom_id="order_done")
    async def done(self, i: discord.Interaction, b: discord.ui.Button):
        if not is_admin(i.user):
            return await i.response.send_message("❌ 관리자만 누를 수 있습니다.", ephemeral=True)
        oid = footer_id(i.message)
        row = get("SELECT * FROM orders WHERE id=?", (oid,))
        if not row or row["status"] != "pending":
            return await i.response.send_message("이미 처리된 주문입니다.", ephemeral=True)
        run("UPDATE orders SET status='done' WHERE id=?", (oid,))
        embed = i.message.embeds[0]
        embed.color = 0x57F287
        embed.add_field(name="처리", value=f"✅ {i.user.mention} 지급 완료", inline=False)
        await i.response.edit_message(embed=embed, view=disabled_view(OrderView))
        try:
            user = await bot.fetch_user(row["user_id"])
            await user.send(f"✅ 주문번호 #{oid} ({row['product']} × {row['qty']}) 지급이 완료되었습니다.")
        except discord.HTTPException:
            pass


class ChargeView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="✅ 입금 확인", style=discord.ButtonStyle.success, custom_id="charge_ok")
    async def ok(self, i: discord.Interaction, b: discord.ui.Button):
        if not is_admin(i.user):
            return await i.response.send_message("❌ 관리자만 누를 수 있습니다.", ephemeral=True)
        row = confirm_charge(footer_id(i.message))
        if not row:
            return await i.response.send_message("이미 처리된 충전입니다.", ephemeral=True)
        embed = i.message.embeds[0]
        embed.color = 0x57F287
        embed.add_field(name="처리", value=f"✅ {i.user.mention} 수동 승인", inline=False)
        await i.response.edit_message(embed=embed, view=disabled_view(ChargeView))
        await notify_charge(row)

    @discord.ui.button(label="❌ 거절", style=discord.ButtonStyle.danger, custom_id="charge_no")
    async def no(self, i: discord.Interaction, b: discord.ui.Button):
        if not is_admin(i.user):
            return await i.response.send_message("❌ 관리자만 누를 수 있습니다.", ephemeral=True)
        cid = footer_id(i.message)
        cur = run("UPDATE charges SET status='rejected' WHERE id=? AND status='pending'", (cid,))
        if cur.rowcount == 0:
            return await i.response.send_message("이미 처리된 충전입니다.", ephemeral=True)
        embed = i.message.embeds[0]
        embed.color = 0xED4245
        embed.add_field(name="처리", value=f"❌ {i.user.mention} 거절", inline=False)
        await i.response.edit_message(embed=embed, view=disabled_view(ChargeView))


async def notify_charge(row):
    try:
        user = await bot.fetch_user(row["user_id"])
        bal = get_points(row["guild_id"], row["user_id"])
        await user.send(f"💳 {row['amount']:,}원 충전이 완료되었습니다. 현재 잔액: **{bal:,}P**")
    except discord.HTTPException:
        pass


# ───────────────────── 입금 자동 확인 (웹훅, 선택) ─────────────────────


async def start_webhook(bot_):
    if not WEBHOOK_SECRET:
        print("[웹훅] WEBHOOK_SECRET 이 없어 자동 입금 확인을 시작하지 않습니다.")
        return
    from aiohttp import web

    async def deposit(request: web.Request):
        if request.headers.get("X-Secret") != WEBHOOK_SECRET:
            return web.json_response({"ok": False, "reason": "unauthorized"}, status=401)
        try:
            data = await request.json()
            name = str(data["name"]).strip()
            amount = int(data["amount"])
        except Exception:
            return web.json_response({"ok": False, "reason": "bad request"}, status=400)
        row = get("SELECT * FROM charges WHERE status='pending' AND depositor=? AND amount=? ORDER BY id LIMIT 1",
                  (name, amount))
        if not row:
            return web.json_response({"ok": False, "reason": "no matching charge"}, status=404)
        done = confirm_charge(row["id"])
        if done:
            await notify_charge(done)
        return web.json_response({"ok": bool(done)})

    app = web.Application()
    app.router.add_post("/deposit", deposit)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", WEBHOOK_PORT).start()
    print(f"[웹훅] 입금 확인 서버 시작: 포트 {WEBHOOK_PORT}")


if __name__ == "__main__":
    bot.run(TOKEN)
