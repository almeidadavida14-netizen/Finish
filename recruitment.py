"""Persistent recruitment applications and approval announcements for EVOM NEXUS."""
from __future__ import annotations
import re, sqlite3
import discord
from message_editor import customized_embed
from discord import app_commands
from config import DATABASE_PATH, STAFF_ROLE_NAMES

CATEGORY_NAME = "🎯・RECRUTAMENTO"
PANEL_CHANNEL = "📋・recrutamento"
INFO_CHANNEL = "🎮・fazer-teste"
ADMIN_NOTICE_CHANNEL_NAME = "candidaturas-adm"

class ApplicationModal(discord.ui.Modal):
    def __init__(self, system, guild_id=None):
        super().__init__(title="Candidatura", timeout=300); self.system=system
        labels=system.get_form_labels(guild_id, "first")
        self.player_name=discord.ui.TextInput(label=labels[0], max_length=80)
        self.ff_id=discord.ui.TextInput(label=labels[1], max_length=32)
        self.whatsapp=discord.ui.TextInput(label=labels[2], placeholder="Ex.: 11987654321", max_length=20)
        for item in (self.player_name,self.ff_id,self.whatsapp): self.add_item(item)
    async def on_submit(self, interaction: discord.Interaction):
        await self.system.create_application(interaction, str(self.player_name), str(self.ff_id), str(self.whatsapp))


class ApplyView(discord.ui.View):
    def __init__(self, system):
        super().__init__(timeout=None); self.system=system
    @discord.ui.button(label="📝 Candidatar-se", style=discord.ButtonStyle.success, custom_id="evom:recruit:apply")
    async def apply(self, interaction: discord.Interaction, button: discord.ui.Button):
        settings=self.system._settings(interaction.guild_id)
        if settings and not settings["enabled"]:
            return await interaction.response.send_message("As candidaturas estão temporariamente bloqueadas.", ephemeral=True)
        await interaction.response.send_modal(ApplicationModal(self.system, interaction.guild_id))

class CandidateView(discord.ui.View):
    """Painel Staff publicado no próprio canal de teste; ações restritas à equipe."""
    ACTIONS = [
        ("👥 Selecionar ADM para jogar", "select_partner", discord.ButtonStyle.primary),
        ("🔁 Transferir responsável", "transfer_owner", discord.ButtonStyle.primary),
        ("✅ Aprovar candidato", "approve", discord.ButtonStyle.success),
        ("❌ Reprovar candidato", "reject", discord.ButtonStyle.danger),
        ("📣 Anunciar", "announce", discord.ButtonStyle.secondary),
        ("📝 Enviar formulário", "second_form", discord.ButtonStyle.primary),
        ("🔐 Enviar ID e senha da sala", "room_credentials", discord.ButtonStyle.success),
    ]
    def __init__(self, system, user_id=None):
        super().__init__(timeout=None)
        self.system = system
        self.user_id = int(user_id) if user_id else None
        for label, action, style in self.ACTIONS:
            self.add_item(CandidateActionButton(system, self.user_id, label, action, style))

    def staff(self, member):
        return bool(getattr(member.guild_permissions, "administrator", False)) or any(
            r.name.casefold() in STAFF_ROLE_NAMES or r.permissions.administrator
            for r in getattr(member, "roles", [])
        )

    async def interaction_check(self, interaction):
        if not self.staff(interaction.user):
            await interaction.response.send_message("Somente ADM/equipe autorizada pode usar o Painel Staff.", ephemeral=True)
            return False
        return True


class CandidateActionButton(discord.ui.Button):
    def __init__(self, system, user_id, label, action, style):
        self.system = system
        self.user_id = int(user_id) if user_id else None
        self.action = action
        super().__init__(label=label, style=style,
                         custom_id=f"nexus:recruit:staff:{self.user_id or 'candidate'}:{action}")

    async def callback(self, interaction: discord.Interaction):
        view = CandidateView(self.system, self.user_id)
        if not view.staff(interaction.user):
            return await interaction.response.send_message("Somente ADM/equipe autorizada pode usar este painel.", ephemeral=True)
        row = self.system.get(self.user_id) if self.user_id else self.system.get_by_channel(interaction.channel_id)
        if not row or row["guild_id"] != interaction.guild_id:
            return await interaction.response.send_message("Candidatura não encontrada.", ephemeral=True)
        action = self.action
        uid = int(row["user_id"])
        responsible = int(row["staff_id"] or 0)
        if action == "select_partner":
            if row["status"] != "testing" or responsible != interaction.user.id:
                return await interaction.response.send_message("Somente o ADM responsável pode selecionar outro ADM durante um teste ativo.", ephemeral=True)
            return await interaction.response.send_message("Selecione o ADM que vai participar do teste. Ele não poderá aprovar nem reprovar.", view=TestPartnerSelectView(self.system, uid, interaction.user.id), ephemeral=True)
        if action == "transfer_owner":
            if row["status"] != "testing":
                return await interaction.response.send_message("Só é possível transferir a responsabilidade de um teste ativo.", ephemeral=True)
            if not (interaction.guild.owner_id == interaction.user.id or interaction.user.guild_permissions.administrator):
                return await interaction.response.send_message("Somente um ADM superior pode transferir a responsabilidade.", ephemeral=True)
            return await interaction.response.send_message("Selecione o novo ADM responsável pelo teste.", view=TestTransferSelectView(self.system, uid, responsible), ephemeral=True)
        if action in ("approve", "reject"):
            if row["status"] != "testing" or responsible != interaction.user.id:
                return await interaction.response.send_message("Somente o ADM responsável por este teste pode aprovar ou reprovar.", ephemeral=True)
            if action == "reject":
                self.system.update(uid, "rejected", interaction.user.id)
                self.system.discard_stage_two(uid)
                self.system.log_event(uid, interaction.guild_id, interaction.channel_id, interaction.user.id, "rejected", "Candidatura reprovada")
                await interaction.response.send_message(f"❌ Candidatura reprovada por {interaction.user.mention}. Este canal será excluído em **30 segundos**.")
            else:
                self.system.approve_with_stage_two(uid, interaction.user.id)
                _, role_message = await self.system.grant_member_role(interaction.guild, uid)
                self.system.log_event(uid, interaction.guild_id, interaction.channel_id, interaction.user.id, "approved", "Candidato aprovado; " + role_message)
                await interaction.response.send_message(f"✅ Candidato aprovado por {interaction.user.mention}.\n{role_message}\nEste canal será excluído em **30 segundos**.")
            test_channel = interaction.guild.get_channel(int(row["channel_id"] or 0))
            if test_channel is not None:
                await self.system.close_later(test_channel, 30)
            return
        if action == "announce":
            if responsible != interaction.user.id:
                return await interaction.response.send_message("Somente o ADM responsável pode anunciar esta candidatura.", ephemeral=True)
            if row["status"] != "approved":
                return await interaction.response.send_message("Primeiro aprove o candidato.", ephemeral=True)
            if self.system.was_announced(uid):
                return await interaction.response.send_message("O anúncio desta candidatura já foi enviado.", ephemeral=True)
            return await interaction.response.send_message("Escolha o canal de destino e clique em **Enviar anúncio**:", view=AnnouncementView(self.system, uid, interaction.user.id), ephemeral=True)
        if action == "second_form":
            if row["status"] != "testing" or responsible != interaction.user.id:
                return await interaction.response.send_message("Somente o ADM responsável pode enviar o formulário.", ephemeral=True)
            channel = interaction.guild.get_channel(int(row["channel_id"] or 0))
            if not channel:
                return await interaction.response.send_message("Canal privado do candidato não encontrado.", ephemeral=True)
            await channel.send(f"{interaction.user.mention} enviou a segunda etapa da candidatura. Preencha o formulário abaixo:", view=SecondStageApplyView(self.system, uid))
            return await interaction.response.send_message("✅ Formulário enviado no canal privado do candidato.", ephemeral=True)
        if action == "room_credentials":
            if row["status"] != "testing" or responsible != interaction.user.id:
                return await interaction.response.send_message("Somente o ADM responsável pode enviar os dados da sala.", ephemeral=True)
            return await interaction.response.send_modal(RoomCredentialsModal(self.system, uid, interaction.user.id))


class SecondStageApplyView(discord.ui.View):
    def __init__(self, system, user_id):
        super().__init__(timeout=None); self.system=system; self.user_id=int(user_id); self.selected_role=None
        self.add_item(SecondStageRoleSelect(system, user_id, self))
        for item in self.children:
            if isinstance(item, discord.ui.Button) and item.label == "📝 Preencher segunda etapa":
                item.custom_id = f"nexus:recruit:second_stage:{self.user_id}"
    @discord.ui.button(label="📝 Preencher segunda etapa", style=discord.ButtonStyle.success, custom_id="nexus:recruit:second_stage")
    async def fill(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message("Este formulário pertence a outro candidato.", ephemeral=True)
        row=self.system.get(self.user_id)
        if not row or row["status"] != "testing":
            return await interaction.response.send_message("Este formulário não está mais disponível.", ephemeral=True)
        if self.selected_role not in ("Rush", "Curandeiro", "Full gás", "Suporte"):
            return await interaction.response.send_message("Primeiro selecione sua função no menu acima.", ephemeral=True)
        await interaction.response.send_modal(SecondStageModal(self.system, self.user_id, interaction.guild_id, self.selected_role))


class SecondStageRoleSelect(discord.ui.Select):
    def __init__(self, system, user_id, parent):
        options=system.get_role_options(system.get(int(user_id))["guild_id"] if system.get(int(user_id)) else None)
        super().__init__(placeholder="Selecione sua função", min_values=1, max_values=1, custom_id=f"nexus:recruit:second_role:{int(user_id)}", options=[discord.SelectOption(label=x[:100],value=x[:100]) for x in options])
        self.system=system; self.user_id=int(user_id); self.parent_view=parent
    async def callback(self, interaction):
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message("Este formulário pertence a outro candidato.",ephemeral=True)
        self.parent_view.selected_role=self.values[0] if self.values else None
        await interaction.response.send_message("Função selecionada. Agora clique em **Preencher segunda etapa**.",ephemeral=True)


class SecondStageModal(discord.ui.Modal):
    def __init__(self, system, user_id, guild_id=None, selected_role=None):
        super().__init__(title="Etapa 2 — Candidatura", timeout=300); self.system=system; self.user_id=int(user_id)
        labels=system.get_form_labels(guild_id, "second")
        self.selected_role = selected_role
        self.age=discord.ui.TextInput(label=labels[0], max_length=3)
        self.experience=discord.ui.TextInput(label=labels[2], max_length=80)
        self.availability=discord.ui.TextInput(label=labels[3], max_length=100)
        for item in (self.age,self.experience,self.availability): self.add_item(item)
    async def on_submit(self, interaction):
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message("Este formulário pertence a outro candidato.", ephemeral=True)
        row=self.system.get(self.user_id)
        if not row or row["status"] != "testing":
            return await interaction.response.send_message("A etapa de teste não está ativa.", ephemeral=True)
        age_value=str(self.age).strip()
        if not age_value.isdigit() or not 1 <= int(age_value) <= 99:
            return await interaction.response.send_message("Informe uma idade válida usando apenas números.", ephemeral=True)
        if self.selected_role not in ("Rush", "Curandeiro", "Full gás", "Suporte"):
            return await interaction.response.send_message("Selecione uma das quatro funções disponíveis antes de preencher.", ephemeral=True)
        self.system.save_stage_two(self.user_id, age_value, self.selected_role, str(self.experience), str(self.availability))
        await interaction.response.send_message("✅ Segunda etapa enviada. A ADM responsável poderá analisar suas respostas.", ephemeral=True)
        admin_channel=discord.utils.get(interaction.guild.text_channels, name=ADMIN_NOTICE_CHANNEL_NAME) if interaction.guild else None
        if admin_channel:
            await admin_channel.send(
                f"📩 **Segunda etapa recebida** · candidato <@{self.user_id}>\n"
                f"**Idade:** {str(self.age)}\n**Função:** {self.selected_role}\n"
                f"**Tempo de jogo:** {str(self.experience)}\n**Disponibilidade:** {str(self.availability)}"
            )


class RoomCredentialsModal(discord.ui.Modal, title="Dados da sala Free Fire"):
    room_id=discord.ui.TextInput(label="ID da sala", max_length=32)
    password=discord.ui.TextInput(label="Senha da sala", max_length=32)
    def __init__(self, system, user_id, staff_id):
        super().__init__(timeout=300); self.system=system; self.user_id=int(user_id); self.staff_id=int(staff_id)
    async def on_submit(self, interaction):
        if interaction.user.id != self.staff_id or not CandidateView(self.system).staff(interaction.user):
            return await interaction.response.send_message("Somente o ADM responsável pode enviar os dados.", ephemeral=True)
        row=self.system.get(self.user_id)
        if not row or row["status"] != "testing" or int(row["staff_id"] or 0) != self.staff_id:
            return await interaction.response.send_message("Você não é mais o responsável por este teste.", ephemeral=True)
        channel=interaction.guild.get_channel(int(row["channel_id"] or 0))
        if not channel:
            return await interaction.response.send_message("Canal do candidato não encontrado.", ephemeral=True)
        await channel.send(f"🔐 **Dados da sala Free Fire**\n**ID:** `{self.room_id}`\n**Senha:** `{self.password}`")
        await interaction.response.send_message("✅ ID e senha enviados no canal privado do candidato.", ephemeral=True)


class TestPartnerSelectView(discord.ui.View):
    def __init__(self, system, user_id, responsible_id):
        super().__init__(timeout=120); self.system=system; self.user_id=user_id; self.responsible_id=responsible_id
        self.add_item(TestPartnerSelect(system, user_id, responsible_id))

class TestPartnerSelect(discord.ui.UserSelect):
    def __init__(self, system, user_id, responsible_id):
        super().__init__(placeholder="Escolha o ADM participante", min_values=1, max_values=1)
        self.system=system; self.user_id=user_id; self.responsible_id=responsible_id
    async def callback(self, interaction):
        if interaction.user.id != self.responsible_id or not CandidateView(self.system).staff(interaction.user):
            return await interaction.response.send_message("Somente o ADM responsável pode selecionar.", ephemeral=True)
        member=self.values[0]
        if not isinstance(member, discord.Member) or not (member.guild_permissions.administrator or any(r.name.casefold() in STAFF_ROLE_NAMES for r in member.roles)):
            return await interaction.response.send_message("Selecione um ADM/staff do servidor.", ephemeral=True)
        row=self.system.get(self.user_id); channel=interaction.guild.get_channel(int(row["channel_id"] or 0)) if row else None
        if not channel: return await interaction.response.send_message("Canal de teste não encontrado.", ephemeral=True)
        await channel.set_permissions(member, view_channel=True, send_messages=True, read_message_history=True, attach_files=True, embed_links=True)
        if isinstance(interaction.channel, discord.Thread) and interaction.channel.is_private():
            try:
                await interaction.channel.add_user(member)
            except (discord.Forbidden, discord.HTTPException):
                return await interaction.response.send_message("Não consegui adicionar o ADM à thread privada. Confira as permissões de gerenciar threads.", ephemeral=True)
        self.system.set_partner(self.user_id, member.id)
        self.system.log_event(self.user_id, interaction.guild_id, channel.id, interaction.user.id, "test_partner_selected", f"ADM participante: {member.id}")
        await interaction.response.send_message(f"✅ {member.mention} foi adicionado ao teste. Ele não pode aprovar ou reprovar.", ephemeral=True)

class TestTransferSelectView(discord.ui.View):
    def __init__(self, system, user_id, old_id):
        super().__init__(timeout=120); self.system=system; self.user_id=user_id; self.old_id=old_id
        self.add_item(TestTransferSelect(system,user_id,old_id))

class TestTransferSelect(discord.ui.UserSelect):
    def __init__(self, system, user_id, old_id):
        super().__init__(placeholder="Escolha o novo responsável", min_values=1, max_values=1)
        self.system=system; self.user_id=user_id; self.old_id=old_id
    async def callback(self, interaction):
        if not (interaction.guild.owner_id == interaction.user.id or interaction.user.guild_permissions.administrator):
            return await interaction.response.send_message("Somente ADM superior pode transferir.", ephemeral=True)
        member=self.values[0]
        if not isinstance(member, discord.Member) or not (member.guild_permissions.administrator or any(r.name.casefold() in STAFF_ROLE_NAMES for r in member.roles)):
            return await interaction.response.send_message("Selecione um ADM/staff.", ephemeral=True)
        if not self.system.transfer_responsible(self.user_id,self.old_id,member.id):
            return await interaction.response.send_message("Não foi possível transferir. Confira se o teste ainda está ativo.", ephemeral=True)
        row=self.system.get(self.user_id); channel=interaction.guild.get_channel(int(row["channel_id"] or 0)) if row else None
        if channel:
            await channel.set_permissions(member, view_channel=True, send_messages=True, read_message_history=True, manage_channels=True, manage_threads=True)
            old=interaction.guild.get_member(self.old_id)
            if old:
                await channel.set_permissions(old, view_channel=False, send_messages=False, read_message_history=False)

            if isinstance(interaction.channel, discord.Thread) and interaction.channel.is_private():
                try:
                    await interaction.channel.add_user(member)
                    if old and old.id != interaction.guild.owner_id:
                        await interaction.channel.remove_user(old)
                except (discord.Forbidden, discord.HTTPException):
                    pass
            async for msg in channel.history(limit=30):
                if msg.author.id == self.system.bot.user.id and msg.embeds:
                    try: await msg.edit(view=None)
                    except discord.HTTPException: pass
                    break
        await interaction.response.send_message(f"✅ Responsabilidade transferida para {member.mention}.",ephemeral=True)


class AnnouncementView(discord.ui.View):
    def __init__(self, system, user_id, staff_id):
        super().__init__(timeout=300)
        self.system = system
        self.user_id = user_id
        self.staff_id = staff_id
        self.channel_id = None

    @discord.ui.select(cls=discord.ui.ChannelSelect, channel_types=[discord.ChannelType.text], placeholder="Selecione o canal do anúncio", min_values=1, max_values=1)
    async def channel(self, interaction: discord.Interaction, select: discord.ui.ChannelSelect):
        if interaction.user.id != self.staff_id or not CandidateView(self.system).staff(interaction.user):
            return await interaction.response.send_message("Somente o ADM responsável pode confirmar.", ephemeral=True)
        self.channel_id = select.values[0].id
        await interaction.response.send_message(f"Canal selecionado: {select.values[0].mention}. Clique em **Enviar anúncio** para publicar.", ephemeral=True)

    @discord.ui.button(label="📣 Enviar anúncio", style=discord.ButtonStyle.success)
    async def send_announcement(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.staff_id or not CandidateView(self.system).staff(interaction.user):
            return await interaction.response.send_message("Somente o ADM responsável pode confirmar.", ephemeral=True)
        if not self.channel_id:
            return await interaction.response.send_message("Selecione um canal primeiro.", ephemeral=True)
        row = self.system.get(self.user_id)
        if not row or row["status"] != "approved":
            return await interaction.response.send_message("A candidatura não está marcada como aprovada.", ephemeral=True)
        if self.system.was_announced(self.user_id):
            return await interaction.response.send_message("Este anúncio já foi enviado.", ephemeral=True)
        if not self.system.reserve_announcement(self.user_id):
            return await interaction.response.send_message("O anúncio já está sendo enviado ou foi concluído.", ephemeral=True)
        channel = interaction.guild.get_channel(self.channel_id)
        if channel is None:
            self.system.release_announcement(self.user_id)
            return await interaction.response.send_message("Não encontrei o canal selecionado. Escolha outro canal.", ephemeral=True)
        member = interaction.guild.get_member(self.user_id)
        staff_id = row["staff_id"] or self.staff_id
        staff = interaction.guild.get_member(staff_id)
        name = discord.utils.escape_markdown(row["player_name"])
        staff_text = staff.mention if staff else f"<@{staff_id}>"
        description = (
            f"Deem as boas-vindas a {member.mention if member else name}!\n\n"
            "O jogador foi **aprovado no teste** e agora faz parte da nossa família.\n\n"
            f"**Nome/Nick:** {name}\n**ID do Free Fire:** `{row['ff_id']}`\n"
            f"**ID do Discord:** `{row['user_id']}`\n**Aprovado por:** {staff_text}"
        )
        embed = customized_embed("new_member_announcement", "🏆 NOVO MEMBRO NA ALL EVOM! 🔥", description, 0xD2192D, member=(member.mention if member else name), name=name, ff_id=row["ff_id"], user_id=row["user_id"], staff=staff_text)
        try:
            message = await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False))
        except (discord.Forbidden, discord.HTTPException) as exc:
            self.system.release_announcement(self.user_id)
            return await interaction.response.send_message(f"Não consegui enviar o anúncio: {exc}", ephemeral=True)
        self.system.finish_announcement(self.user_id)
        self.system.log_event(self.user_id, interaction.guild_id, row["channel_id"], interaction.user.id, "announcement_sent", f"Anúncio publicado no canal {channel.id}; mensagem {message.id}")
        await interaction.response.send_message(f"Anúncio enviado em {channel.mention}.", ephemeral=True)

class AnalysisNoticeView(discord.ui.View):
    """Controles do aviso de candidatura no canal privado da ADM."""
    def __init__(self, system, user_id):
        super().__init__(timeout=None)
        self.system = system
        self.user_id = int(user_id)
        for item in self.children:
            if isinstance(item, discord.ui.Button) and item.custom_id:
                item.custom_id = f"{item.custom_id}:{self.user_id}"

    def staff(self, member):
        return bool(getattr(member.guild_permissions, "administrator", False)) or any(
            r.name.casefold() in STAFF_ROLE_NAMES or r.permissions.administrator
            for r in getattr(member, "roles", [])
        )

    async def _remove_notice(self, interaction):
        """Remove imediatamente a mensagem original da candidatura após o aceite."""
        try:
            await interaction.message.delete()
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            # Se o bot não tiver Manage Messages, mantém o fluxo e registra no log.
            self.system.log_event(
                self.user_id,
                interaction.guild_id,
                interaction.channel_id,
                interaction.user.id,
                "notice_delete_failed",
                "Não foi possível apagar a mensagem original da candidatura; confira a permissão Gerenciar Mensagens.",
            )

    @discord.ui.button(label="✅ Aceitar para análise", style=discord.ButtonStyle.primary, custom_id="evom:recruit:claim_analysis")
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.staff(interaction.user):
            return await interaction.response.send_message("Somente ADM/equipe autorizada pode aceitar candidaturas.", ephemeral=True)
        row = self.system.get(self.user_id)
        if not row or row["guild_id"] != interaction.guild_id:
            return await interaction.response.send_message("Candidatura não encontrada.", ephemeral=True)
        # A reserva no SQLite acontece antes da criação: cliques concorrentes não
        # conseguem abrir dois canais para a mesma candidatura.
        if not self.system.reserve_test(self.user_id, interaction.user.id):
            return await interaction.response.send_message("Esta candidatura já foi processada ou está sendo aceita por outro administrador.", ephemeral=True)
        await interaction.response.defer(ephemeral=True, thinking=True)
        channel = None
        created_channel = False
        try:
            guild = interaction.guild
            category = discord.utils.get(guild.categories, name=CATEGORY_NAME)
            if category is None:
                category = await guild.create_category(CATEGORY_NAME, reason="Canal privado de teste de candidatura")
            safe = re.sub(r"[^a-z0-9-]+", "-", str(row["player_name"]).lower()).strip("-")[:55] or str(self.user_id)
            overwrites = {
                guild.default_role: discord.PermissionOverwrite(view_channel=False),
            }
            candidate = guild.get_member(self.user_id)
            if candidate is None:
                candidate = await guild.fetch_member(self.user_id)
            overwrites[candidate] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, attach_files=True, embed_links=True)
            # O candidato e a equipe veem o canal. Os controles do Painel Staff
            # ficam na mensagem, mas cada interação é validada como exclusiva da equipe.
            for role in guild.roles:
                if role.name.casefold() in STAFF_ROLE_NAMES or role.permissions.administrator:
                    overwrites[role] = discord.PermissionOverwrite(
                        view_channel=True, send_messages=True, read_message_history=True
                    )
            responsible = guild.get_member(interaction.user.id)
            if responsible:
                overwrites[responsible] = discord.PermissionOverwrite(
                    view_channel=True, send_messages=True, read_message_history=True,
                    manage_channels=True
                )
            if guild.me:
                overwrites[guild.me] = discord.PermissionOverwrite(
                    view_channel=True, send_messages=True, read_message_history=True,
                    manage_channels=True
                )
            channel = guild.get_channel(int(row["channel_id"] or 0)) if row["channel_id"] else None
            if channel is None and row["channel_id"]:
                try:
                    channel = await guild.fetch_channel(int(row["channel_id"]))
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    channel = None
            if channel is None:
                channel = await guild.create_text_channel(f"teste-{safe}", category=category, overwrites=overwrites, reason=f"Teste da candidatura {self.user_id}")
                created_channel = True
            else:
                await channel.edit(overwrites=overwrites, reason="Atualizar permissões do canal de teste existente")
            embed = discord.Embed(title="📝 Candidatura para avaliação", color=discord.Color.orange())
            embed.add_field(name="Candidato", value=f"<@{self.user_id}>", inline=False)
            embed.add_field(name="Nome/Nick", value=str(row["player_name"]), inline=True)
            embed.add_field(name="UID Free Fire", value=str(row["ff_id"]), inline=True)
            embed.add_field(name="WhatsApp", value=str(row["whatsapp"] or "Não informado"), inline=True)
            embed.add_field(name="Responsável", value=interaction.user.mention, inline=False)
            updated_panel = False
            try:
                async for old_message in channel.history(limit=30):
                    if old_message.author.id == self.system.bot.user.id and old_message.embeds and old_message.embeds[0].title == "📝 Candidatura para avaliação":
                        await old_message.edit(embed=embed, view=None)
                        updated_panel = True
                        break
            except (discord.Forbidden, discord.HTTPException):
                pass
            if not updated_panel:
                await channel.send(embed=embed)
            self.system.finish_test_reservation(self.user_id, channel.id, interaction.user.id)
            # Localiza a caixa de entrada administrativa pelo nome configurado.
            admin_channel = discord.utils.get(guild.text_channels, name=ADMIN_NOTICE_CHANNEL_NAME)
            if admin_channel is None:
                try:
                    admin_channel = await self.system.ensure_admin_notice_channel(guild)
                except (discord.Forbidden, discord.HTTPException):
                    admin_channel = None
            try:
                await self.system.ensure_staff_panel(guild, self.user_id, interaction.user)
            except (discord.Forbidden, discord.HTTPException) as panel_error:
                # A falha ao publicar o painel não desfaz a candidatura já criada.
                self.system.log_event(self.user_id, guild.id, channel.id, interaction.user.id,
                                      "staff_panel_publish_failed", type(panel_error).__name__)
            # Apaga o aviso original do canal candidaturas-adm somente depois
            # que o canal privado foi criado e a candidatura foi reservada com sucesso.
            await self._remove_notice(interaction)
            self.system.log_event(self.user_id, guild.id, channel.id, interaction.user.id, "test_accepted", "Canal privado criado após aceite da ADM")
            await interaction.followup.send(f"✅ Teste aceito. Canal privado criado: {channel.mention}", ephemeral=True)
        except Exception as exc:
            if channel is not None and created_channel:
                try:
                    await channel.delete(reason="Falha ao concluir a criação do canal de teste")
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    pass
            self.system.release_test_reservation(self.user_id)
            self.system.log_event(self.user_id, interaction.guild_id, None, interaction.user.id, "test_accept_failed", type(exc).__name__)
            await interaction.followup.send("❌ Não foi possível criar o canal de teste. A candidatura voltou a aguardar análise.", ephemeral=True)
            raise

    @discord.ui.button(label="❌ Recusar candidatura", style=discord.ButtonStyle.danger, custom_id="evom:recruit:decline_notice")
    async def decline_notice(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.staff(interaction.user):
            return await interaction.response.send_message("Somente ADM/equipe autorizada pode recusar candidaturas.", ephemeral=True)
        row = self.system.get(self.user_id)
        if not row or row["guild_id"] != interaction.guild_id:
            return await interaction.response.send_message("Candidatura não encontrada.", ephemeral=True)
        if not self.system.reject_pending(self.user_id, interaction.user.id):
            return await interaction.response.send_message("Esta candidatura já foi processada.", ephemeral=True)
        self.system.log_event(self.user_id, interaction.guild_id, None, interaction.user.id, "rejected_for_test", "Candidatura recusada no aviso da ADM")
        try:
            await interaction.message.delete()
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            pass
        await interaction.response.send_message("❌ Candidatura recusada. Nenhum canal privado foi criado.", ephemeral=True)


class RecruitmentSystem:
    def __init__(self, bot):
        self.bot=bot
        self.db=sqlite3.connect(DATABASE_PATH, check_same_thread=False)
        self.db.row_factory=sqlite3.Row
        self.db.execute('''CREATE TABLE IF NOT EXISTS recruitment_applications (user_id INTEGER PRIMARY KEY, guild_id INTEGER NOT NULL, channel_id INTEGER, player_name TEXT NOT NULL, ff_id TEXT NOT NULL, test_mode TEXT NOT NULL, availability TEXT NOT NULL, status TEXT NOT NULL, staff_id INTEGER, created_at TEXT DEFAULT CURRENT_TIMESTAMP)''')
        self.db.execute('''CREATE TABLE IF NOT EXISTS recruitment_events (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, guild_id INTEGER NOT NULL, channel_id INTEGER, staff_id INTEGER, event_type TEXT NOT NULL, details TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP)''')
        self.db.commit()
        self.ensure_assignment_column()
        self.ensure_whatsapp_column()
        self.ensure_partner_column()
        self.ensure_announcement_columns()
        self.ensure_recruitment_columns()
        self.db.execute("""CREATE TABLE IF NOT EXISTS recruitment_stage_two (
            user_id INTEGER PRIMARY KEY, age TEXT NOT NULL, game_role TEXT NOT NULL,
            experience TEXT NOT NULL, availability TEXT NOT NULL, submitted_at TEXT DEFAULT CURRENT_TIMESTAMP)""")
        self.db.execute("""CREATE TABLE IF NOT EXISTS recruitment_settings (
            guild_id INTEGER PRIMARY KEY, enabled INTEGER NOT NULL DEFAULT 1,
            panel_title TEXT, panel_description TEXT, info_title TEXT, info_description TEXT,
            first_form_json TEXT, second_form_json TEXT, role_options_json TEXT)""")
        setting_columns={r[1] for r in self.db.execute("PRAGMA table_info(recruitment_settings)").fetchall()}
        if "role_options_json" not in setting_columns:
            self.db.execute("ALTER TABLE recruitment_settings ADD COLUMN role_options_json TEXT")
        self.db.commit()
        self.remove_legacy_gamer_center()
    def remove_legacy_gamer_center(self):
        """Back up the current SQLite file, then remove only retired Gamer Center tables."""
        tables=[r[0] for r in self.db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'central_gamer_%'").fetchall()]
        if not tables: return
        from pathlib import Path
        from datetime import datetime
        backup_dir=Path(DATABASE_PATH).parent / "backups"
        backup_dir.mkdir(parents=True,exist_ok=True)
        backup_path=backup_dir / f"pre_gamer_center_removal_{datetime.now().strftime('%Y%m%d_%H%M%S')}.sqlite3"
        target=sqlite3.connect(str(backup_path))
        try: self.db.backup(target)
        finally: target.close()
        with self.db:
            for table in tables:
                if table.startswith("central_gamer_"):
                    self.db.execute(f'DROP TABLE IF EXISTS "{table}"')

    def ensure_recruitment_columns(self):
        columns={r[1] for r in self.db.execute("PRAGMA table_info(recruitment_applications)").fetchall()}
        for name in ("stage_two_age", "stage_two_role", "stage_two_experience", "stage_two_availability"):
            if name not in columns: self.db.execute(f"ALTER TABLE recruitment_applications ADD COLUMN {name} TEXT")
        if "staff_thread_id" not in columns:
            self.db.execute("ALTER TABLE recruitment_applications ADD COLUMN staff_thread_id INTEGER")
        self.db.commit()

    def _settings(self, guild_id):
        if not guild_id: return None
        self.db.execute("INSERT OR IGNORE INTO recruitment_settings(guild_id) VALUES(?)", (int(guild_id),)); self.db.commit()
        return self.db.execute("SELECT * FROM recruitment_settings WHERE guild_id=?", (int(guild_id),)).fetchone()

    def get_form_labels(self, guild_id, kind):
        import json
        defaults={"first":["Seu nome / nick","ID do Free Fire","WhatsApp com DDD"],"second":["Idade","Função no jogo","Há quanto tempo joga?","Disponibilidade para jogar"]}
        row=self._settings(guild_id)
        if not row: return defaults[kind]
        raw=row["first_form_json"] if kind=="first" else row["second_form_json"]
        try:
            values=json.loads(raw) if raw else defaults[kind]
            if isinstance(values,list) and len(values)==len(defaults[kind]):
                return [str(v)[:45] or defaults[kind][i] for i,v in enumerate(values)]
        except (ValueError,TypeError): pass
        return defaults[kind]

    def get_role_options(self, guild_id):
        import json
        row=self._settings(guild_id)
        return ["Rush", "Curandeiro", "Full gás", "Suporte"]

    def save_role_options(self, guild_id, values):
        import json
        cleaned=[]
        for value in values:
            for part in str(value).split(","):
                part=part.strip()[:100]
                if part and part not in cleaned: cleaned.append(part)
        cleaned = [v for v in ("Rush", "Curandeiro", "Full gás", "Suporte") if v in cleaned]
        if not cleaned: cleaned = ["Rush", "Curandeiro", "Full gás", "Suporte"]
        self._settings(guild_id)
        with self.db: self.db.execute("UPDATE recruitment_settings SET role_options_json=? WHERE guild_id=?", (json.dumps(cleaned,ensure_ascii=False),int(guild_id)))

    def save_form_labels(self, guild_id, kind, values):
        import json
        if kind not in ("first","second"): return
        column="first_form_json" if kind=="first" else "second_form_json"
        self._settings(guild_id)
        with self.db: self.db.execute(f"UPDATE recruitment_settings SET {column}=? WHERE guild_id=?", (json.dumps(values,ensure_ascii=False),int(guild_id)))

    def save_messages(self, guild_id, panel_title, panel_description, info_title, info_description):
        self._settings(guild_id)
        with self.db: self.db.execute("UPDATE recruitment_settings SET panel_title=?,panel_description=?,info_title=?,info_description=? WHERE guild_id=?", (panel_title[:100],panel_description[:1000],info_title[:100],info_description[:1000],int(guild_id)))

    def toggle_enabled(self, guild_id):
        row=self._settings(guild_id)
        enabled=0 if row and row["enabled"] else 1
        with self.db: self.db.execute("UPDATE recruitment_settings SET enabled=? WHERE guild_id=?", (enabled,int(guild_id)))
        return bool(enabled)

    async def refresh_panel(self, guild):
        if guild is None: return
        category=discord.utils.get(guild.categories,name=CATEGORY_NAME)
        panel=discord.utils.get(category.text_channels,name=PANEL_CHANNEL) if category else None
        if not panel: return
        settings=self._settings(guild.id)
        enabled=bool(settings["enabled"]) if settings else True
        title=(settings["panel_title"] if settings and settings["panel_title"] else "🎯 Recrutamento")
        description=(settings["panel_description"] if settings and settings["panel_description"] else "Quer entrar para a guilda? Clique abaixo e preencha sua candidatura. A equipe falará com você por um canal privado.")
        embed=customized_embed("recruitment_panel",title,description,0xFF0000)
        async for message in panel.history(limit=20):
            if message.author.id==self.bot.user.id and message.embeds:
                await message.edit(embed=embed,view=ApplyView(self) if enabled else None)
                break
        category=discord.utils.get(guild.categories,name=CATEGORY_NAME)
        info=discord.utils.get(category.text_channels,name=INFO_CHANNEL) if category else None
        if info:
            info_title=settings["info_title"] if settings and settings["info_title"] else "🎮 Como funciona o teste"
            info_description=settings["info_description"] if settings and settings["info_description"] else "Aguarde a equipe orientar seu teste e acompanhe as instruções no canal privado."
            info_embed=customized_embed("recruitment_info",info_title,info_description,0x5865F2)
            async for message in info.history(limit=20):
                if message.author.id==self.bot.user.id and message.embeds:
                    await message.edit(embed=info_embed); break

    def save_stage_two(self, user_id, age, game_role, experience, availability):
        with self.db:
            self.db.execute("INSERT INTO recruitment_stage_two(user_id,age,game_role,experience,availability) VALUES(?,?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET age=excluded.age,game_role=excluded.game_role,experience=excluded.experience,availability=excluded.availability,submitted_at=CURRENT_TIMESTAMP", (int(user_id), age[:3], game_role[:80], experience[:80], availability[:100]))

    def approve_with_stage_two(self, user_id, staff_id):
        with self.db:
            self.db.execute("UPDATE recruitment_applications SET status='approved',staff_id=?,stage_two_age=(SELECT age FROM recruitment_stage_two WHERE user_id=?),stage_two_role=(SELECT game_role FROM recruitment_stage_two WHERE user_id=?),stage_two_experience=(SELECT experience FROM recruitment_stage_two WHERE user_id=?),stage_two_availability=(SELECT availability FROM recruitment_stage_two WHERE user_id=?) WHERE user_id=?", (int(staff_id), int(user_id), int(user_id), int(user_id), int(user_id), int(user_id)))
            self.db.execute("DELETE FROM recruitment_stage_two WHERE user_id=?", (int(user_id),))

    def get_stage_two(self, user_id):
        return self.db.execute("SELECT * FROM recruitment_stage_two WHERE user_id=?", (int(user_id),)).fetchone()

    def discard_stage_two(self, user_id):
        with self.db: self.db.execute("DELETE FROM recruitment_stage_two WHERE user_id=?", (int(user_id),))

    def ensure_assignment_column(self):
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(recruitment_applications)").fetchall()}
        if "assigned_staff_id" not in columns:
            self.db.execute("ALTER TABLE recruitment_applications ADD COLUMN assigned_staff_id INTEGER")
            self.db.commit()
    def ensure_whatsapp_column(self):
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(recruitment_applications)").fetchall()}
        if "whatsapp" not in columns:
            self.db.execute("ALTER TABLE recruitment_applications ADD COLUMN whatsapp TEXT NOT NULL DEFAULT ''")
            self.db.commit()
    def ensure_partner_column(self):
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(recruitment_applications)").fetchall()}
        if "test_partner_id" not in columns:
            self.db.execute("ALTER TABLE recruitment_applications ADD COLUMN test_partner_id INTEGER")
            self.db.commit()

    def set_partner(self, user_id, partner_id):
        with self.db:
            self.db.execute("UPDATE recruitment_applications SET test_partner_id=? WHERE user_id=? AND status='testing'", (int(partner_id), int(user_id)))

    def transfer_responsible(self, user_id, old_id, new_id):
        with self.db:
            cur = self.db.execute("UPDATE recruitment_applications SET staff_id=?, assigned_staff_id=? WHERE user_id=? AND staff_id=? AND status='testing'", (int(new_id), int(new_id), int(user_id), int(old_id)))
            return cur.rowcount == 1

    def ensure_announcement_columns(self):
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(recruitment_applications)").fetchall()}
        if "announcement_pending" not in columns:
            self.db.execute("ALTER TABLE recruitment_applications ADD COLUMN announcement_pending INTEGER NOT NULL DEFAULT 0")
        if "announcement_sent" not in columns:
            self.db.execute("ALTER TABLE recruitment_applications ADD COLUMN announcement_sent INTEGER NOT NULL DEFAULT 0")
        self.db.execute("UPDATE recruitment_applications SET announcement_sent=1 WHERE user_id IN (SELECT user_id FROM recruitment_events WHERE event_type='announcement_sent')")
        self.db.commit()

    def reserve_announcement(self, user_id):
        with self.db:
            cur=self.db.execute("UPDATE recruitment_applications SET announcement_pending=1 WHERE user_id=? AND status='approved' AND announcement_sent=0 AND announcement_pending=0",(int(user_id),))
            return cur.rowcount == 1

    def finish_announcement(self,user_id):
        with self.db:
            self.db.execute("UPDATE recruitment_applications SET announcement_pending=0,announcement_sent=1 WHERE user_id=?",(int(user_id),))

    def release_announcement(self,user_id):
        with self.db:
            self.db.execute("UPDATE recruitment_applications SET announcement_pending=0 WHERE user_id=? AND announcement_sent=0",(int(user_id),))

    def reserve_test(self, user_id, staff_id):
        with self.db:
            cur = self.db.execute("UPDATE recruitment_applications SET status='creating_test', assigned_staff_id=? WHERE user_id=? AND status='pending'", (int(staff_id), int(user_id)))
            return cur.rowcount == 1

    def finish_test_reservation(self, user_id, channel_id, staff_id):
        with self.db:
            cur = self.db.execute("UPDATE recruitment_applications SET status='testing', channel_id=?, staff_id=? WHERE user_id=? AND status='creating_test' AND assigned_staff_id=?", (int(channel_id), int(staff_id), int(user_id), int(staff_id)))
            if cur.rowcount != 1:
                raise RuntimeError("A reserva da candidatura foi perdida")

    def release_test_reservation(self, user_id):
        with self.db:
            self.db.execute("UPDATE recruitment_applications SET status='pending', assigned_staff_id=NULL WHERE user_id=? AND status='creating_test'", (int(user_id),))

    def reject_pending(self, user_id, staff_id):
        with self.db:
            cur = self.db.execute("UPDATE recruitment_applications SET status='rejected', staff_id=? WHERE user_id=? AND status='pending'", (int(staff_id), int(user_id)))
            return cur.rowcount == 1

    def search_applications(self, guild_id, query):
        q = str(query).strip()
        if not q:
            return []
        like = f"%{q}%"
        digits = re.sub(r"\D", "", q)
        phone_like = f"%{digits}%" if digits else like
        return self.db.execute(
            "SELECT user_id, player_name, ff_id, whatsapp, status, created_at, channel_id, stage_two_age, stage_two_role, stage_two_experience, stage_two_availability FROM recruitment_applications WHERE guild_id=? AND (ff_id LIKE ? OR whatsapp LIKE ? OR player_name LIKE ? OR CAST(user_id AS TEXT) LIKE ?) ORDER BY created_at DESC LIMIT 10",
            (int(guild_id), like, phone_like, like, like),
        ).fetchall()
    def remove_application(self, guild_id, user_id, staff_id):
        row = self.db.execute("SELECT channel_id, player_name, status FROM recruitment_applications WHERE guild_id=? AND user_id=?", (int(guild_id), int(user_id))).fetchone()
        if not row:
            return None
        self.log_event(user_id, guild_id, row["channel_id"], staff_id, "application_removed", "Candidatura removida pelo painel de candidatos")
        self.db.execute("DELETE FROM recruitment_applications WHERE guild_id=? AND user_id=?", (int(guild_id), int(user_id)))
        self.discard_stage_two(user_id)
        self.db.commit()
        return row
    def get_assigned(self, user_id):
        row = self.db.execute("SELECT assigned_staff_id FROM recruitment_applications WHERE user_id=?", (user_id,)).fetchone()
        return row[0] if row else None
    def assign(self, user_id, staff_id):
        self.db.execute("UPDATE recruitment_applications SET assigned_staff_id=? WHERE user_id=? AND assigned_staff_id IS NULL", (staff_id, user_id))
        self.db.commit()
    def claim_assignment(self, user_id, staff_id):
        self.db.execute("UPDATE recruitment_applications SET assigned_staff_id=? WHERE user_id=? AND assigned_staff_id IS NULL", (staff_id, user_id))
        self.db.commit()
        return self.get_assigned(user_id)
    def clear_assignment(self, user_id, staff_id):
        self.db.execute("UPDATE recruitment_applications SET assigned_staff_id=NULL WHERE user_id=? AND assigned_staff_id=?", (user_id, staff_id))
        self.db.commit()
    async def setup(self):
        self.bot.add_view(ApplyView(self))
        pending = self.db.execute("SELECT user_id FROM recruitment_applications WHERE status='pending'").fetchall()
        for row in pending:
            self.bot.add_view(AnalysisNoticeView(self, int(row["user_id"])))
        active_admin_panels = self.db.execute("SELECT user_id,status,guild_id FROM recruitment_applications WHERE status IN ('testing','approved')").fetchall()
        for row in active_admin_panels:
            self.bot.add_view(CandidateView(self, int(row["user_id"])))
            if row["status"] == "testing":
                self.bot.add_view(SecondStageApplyView(self, int(row["user_id"])))
        for guild in self.bot.guilds:
            await self.ensure_panel(guild)
            await self.ensure_admin_notice_channel(guild)
            for row in active_admin_panels:
                if int(row["guild_id"]) == guild.id:
                    try:
                        await self.ensure_staff_panel(guild, int(row["user_id"]))
                    except (discord.Forbidden, discord.HTTPException):
                        continue

    async def ensure_staff_panel(self, guild, user_id, responsible_member=None):
        """Publish one persistent button panel directly in the candidate test channel."""
        row = self.get(user_id)
        if not row or not row["channel_id"]:
            return None
        channel = guild.get_channel(int(row["channel_id"]))
        if channel is None:
            try:
                channel = await guild.fetch_channel(int(row["channel_id"]))
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                return None
        panel_text = (
            "🛡️ **PAINEL STAFF**\n"
            f"Gerenciamento da avaliação de <@{int(user_id)}>. "
            "Os botões abaixo são visíveis no canal, mas somente ADMs autorizados podem utilizá-los."
        )
        panel_message = None
        async for message in channel.history(limit=50):
            if message.author.id == self.bot.user.id and message.components:
                content = message.content or ""
                if "PAINEL STAFF" in content or "Painel Staff" in content:
                    panel_message = message
                    break
        view = CandidateView(self, int(user_id))
        if panel_message:
            await panel_message.edit(content=panel_text, view=view)
        else:
            await channel.send(panel_text, view=view)
        return channel

    @staticmethod
    def _is_staff_member(member):
        return bool(getattr(member.guild_permissions, "administrator", False)) or any(
            r.name.casefold() in STAFF_ROLE_NAMES or r.permissions.administrator
            for r in getattr(member, "roles", [])
        )

    async def ensure_admin_notice_channel(self, guild):
        """Find or recreate the private staff inbox for new applications."""
        channel = discord.utils.get(guild.text_channels, name=ADMIN_NOTICE_CHANNEL_NAME)
        if channel is not None:
            try:
                await channel.set_permissions(guild.default_role, view_channel=False)
                for role in guild.roles:
                    if role.name.casefold() in STAFF_ROLE_NAMES or role.permissions.administrator:
                        await channel.set_permissions(role, view_channel=True, send_messages=True,
                                                      read_message_history=True, manage_messages=True)
                if guild.me:
                    await channel.set_permissions(guild.me, view_channel=True, send_messages=True,
                                                  read_message_history=True, manage_messages=True)
            except (discord.Forbidden, discord.HTTPException):
                pass
            return channel
        category = discord.utils.get(guild.categories,name=CATEGORY_NAME)
        if category is None:
            category = await guild.create_category(CATEGORY_NAME, reason="Restaurar categoria de recrutamento")
        overwrites = {guild.default_role: discord.PermissionOverwrite(view_channel=False)}
        bot_member = guild.me
        if bot_member is not None:
            overwrites[bot_member] = discord.PermissionOverwrite(
                view_channel=True, send_messages=True, read_message_history=True,
                embed_links=True, manage_messages=True
            )
        for role in guild.roles:
            if role.name.casefold() in STAFF_ROLE_NAMES or role.permissions.administrator:
                overwrites[role] = discord.PermissionOverwrite(
                    view_channel=True, send_messages=True, read_message_history=True,
                    embed_links=True, manage_messages=True
                )
        channel = await guild.create_text_channel(
            ADMIN_NOTICE_CHANNEL_NAME, category=category, overwrites=overwrites,
            topic="Recebimento privado das candidaturas para análise da equipe.",
            reason="Restaurar canal de análise das candidaturas"
        )
        await channel.send(embed=discord.Embed(
            title="📥 Análise de candidaturas",
            description=("As novas inscrições serão enviadas aqui para a equipe analisar.\n"
                         "Use **Aceitar para análise** para criar o canal privado de teste ou **Recusar candidatura**."),
            color=discord.Color.orange()
        ))
        return channel

    async def ensure_panel(self, guild):
        category=discord.utils.get(guild.categories,name=CATEGORY_NAME)
        if not category: category=await guild.create_category(CATEGORY_NAME, reason="Configurar recrutamento ALL EVOM")
        panel=discord.utils.get(category.text_channels,name=PANEL_CHANNEL)
        settings=self._settings(guild.id)
        enabled=bool(settings["enabled"]) if settings else True
        panel_title=settings["panel_title"] if settings and settings["panel_title"] else "🎯 Recrutamento"
        panel_description=settings["panel_description"] if settings and settings["panel_description"] else "Quer entrar para a guilda? Clique abaixo e preencha sua candidatura. A equipe falará com você por um canal privado."
        if not panel:
            panel=await guild.create_text_channel(PANEL_CHANNEL,category=category)
        existing_panel=False
        async for message in panel.history(limit=20):
            if message.author.id==self.bot.user.id and message.embeds:
                await message.edit(embed=customized_embed("recruitment_panel",panel_title,panel_description,0xFF0000),view=ApplyView(self) if enabled else None)
                existing_panel=True; break
        if not existing_panel:
            await panel.send(embed=customized_embed("recruitment_panel",panel_title,panel_description,0xFF0000),view=ApplyView(self) if enabled else None)
        info=discord.utils.get(category.text_channels,name=INFO_CHANNEL)
        if not info:
            info=await guild.create_text_channel(INFO_CHANNEL,category=category)
            info_title=settings["info_title"] if settings and settings["info_title"] else "🎮 Como funciona o teste"
            info_description=settings["info_description"] if settings and settings["info_description"] else "Aguarde a equipe orientar seu teste e acompanhe as instruções no canal privado."
            await info.send(embed=customized_embed("recruitment_info",info_title,info_description,0x5865F2))
    async def create_application(self, interaction, name, ff_id, whatsapp):
        existing = self.get(interaction.user.id)
        if existing and existing["status"] in ("pending", "creating_test", "testing"):
            return await interaction.response.send_message("Você já tem uma candidatura em andamento aguardando a equipe.", ephemeral=True)
        self.db.execute("DELETE FROM recruitment_events WHERE user_id=? AND event_type='announcement_sent'", (interaction.user.id,))
        self.discard_stage_two(interaction.user.id)
        self.db.execute(
            """INSERT INTO recruitment_applications(user_id,guild_id,channel_id,player_name,ff_id,test_mode,availability,status,staff_id,whatsapp)
            VALUES(?,?,NULL,?,?,'Não informado','A definir na segunda etapa','pending',NULL,?)
            ON CONFLICT(user_id) DO UPDATE SET guild_id=excluded.guild_id,channel_id=NULL,player_name=excluded.player_name,
            ff_id=excluded.ff_id,test_mode='Não informado',availability='A definir na segunda etapa',status='pending',staff_id=NULL,
            assigned_staff_id=NULL,test_partner_id=NULL,whatsapp=excluded.whatsapp,announcement_pending=0,announcement_sent=0,
            stage_two_age=NULL,stage_two_role=NULL,stage_two_experience=NULL,stage_two_availability=NULL""",
            (interaction.user.id, interaction.guild.id, name, ff_id, whatsapp),
        )
        self.db.commit()
        self.log_event(interaction.user.id, interaction.guild.id, None, None, "application_created", "Candidatura criada; aguardando decisão da ADM")
        try:
            notice_channel = await self.ensure_admin_notice_channel(interaction.guild)
        except (discord.Forbidden, discord.HTTPException):
            notice_channel = None
        if notice_channel is not None:
            notice_description=(f"**Candidato:** {interaction.user.mention}\n**Nome/Nick:** {discord.utils.escape_markdown(name)}\n"
                             f"**ID Free Fire:** `{ff_id}`\n**WhatsApp:** `{discord.utils.escape_markdown(whatsapp)}`\n"
                             "**Nenhum canal de teste foi criado ainda.**")
            notice = customized_embed("application_admin_notice", "📥 Nova candidatura para analisar", notice_description, 0xFFA500,
                candidate=interaction.user.mention, name=discord.utils.escape_markdown(name), ff_id=ff_id,
                whatsapp=discord.utils.escape_markdown(whatsapp))
            try:
                await notice_channel.send(embed=notice, view=AnalysisNoticeView(self, interaction.user.id))
                self.log_event(interaction.user.id, interaction.guild.id, None, None, "admin_notice_sent", f"Aviso enviado ao canal {notice_channel.id}")
            except (discord.Forbidden, discord.HTTPException):
                self.log_event(interaction.user.id, interaction.guild.id, None, None, "admin_notice_failed", f"Falha ao enviar aviso ao canal {notice_channel.id}")
        else:
            self.log_event(interaction.user.id, interaction.guild.id, None, None, "admin_notice_failed", "Canal privado de análise não encontrado/acessível")
        await interaction.response.send_message("✅ Candidatura enviada para análise da ADM. O canal privado de teste será criado somente se ela aceitar.", ephemeral=True)

    async def grant_member_role(self, guild, user_id):
        """Grant the diamond Membro Oficial role after an ADM approves an application."""
        if guild is None:
            return False, "⚠️ Não foi possível identificar o servidor para atribuir o cargo."
        role = discord.utils.get(guild.roles, name="💎 Membro Oficial") or discord.utils.get(guild.roles, name="Membro Oficial")
        try:
            if role is None:
                role = await guild.create_role(name="💎 Membro Oficial", reason="Cargo para candidaturas aprovadas da EVOM NEXUS")
            elif role.name == "Membro Oficial":
                await role.edit(name="💎 Membro Oficial", reason="Padronizar nome do cargo Membro Oficial")
            member = guild.get_member(int(user_id))
            if member is None:
                try:
                    member = await guild.fetch_member(int(user_id))
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    member = None
            if member is None:
                return False, "⚠️ Candidatura aprovada, mas a pessoa não foi encontrada no servidor; o cargo precisa ser atribuído quando ela estiver no servidor."
            if role not in member.roles:
                await member.add_roles(role, reason="Candidatura Membro Oficial aprovada pela equipe")
            return True, f"💎 Cargo **{role.name}** atribuído ao candidato."
        except (discord.Forbidden, discord.HTTPException) as exc:
            return False, f"⚠️ A candidatura foi salva, mas não consegui atribuir o cargo automaticamente ({type(exc).__name__}). Confira a permissão Gerenciar Cargos e a posição do cargo do bot."

    def list_applications(self, guild_id, limit=20):
        return self.db.execute(
            "SELECT user_id, player_name, ff_id, status, created_at FROM recruitment_applications WHERE guild_id=? ORDER BY created_at DESC LIMIT ?",
            (int(guild_id), int(limit)),
        ).fetchall()

    def get(self,user_id):
        return self.db.execute('SELECT * FROM recruitment_applications WHERE user_id=?',(user_id,)).fetchone()
    def get_by_channel(self,channel_id):
        return self.db.execute('SELECT * FROM recruitment_applications WHERE channel_id=?',(channel_id,)).fetchone()
    def update(self,user_id,status,staff_id):
        self.db.execute('UPDATE recruitment_applications SET status=?,staff_id=? WHERE user_id=?',(status,staff_id,user_id)); self.db.commit()
    def log_event(self, user_id, guild_id, channel_id, staff_id, event_type, details=""):
        self.db.execute(
            "INSERT INTO recruitment_events(user_id,guild_id,channel_id,staff_id,event_type,details) VALUES(?,?,?,?,?,?)",
            (user_id, guild_id, channel_id, staff_id, event_type, details),
        )
        self.db.commit()

    def was_announced(self, user_id):
        row=self.db.execute("SELECT announcement_sent FROM recruitment_applications WHERE user_id=?",(int(user_id),)).fetchone()
        return bool(row and row[0])

    async def close_later(self, channel, seconds=30):
        import asyncio
        await asyncio.sleep(seconds)
        try:
            await channel.delete(reason=f"Resultado da candidatura; limpeza automática após {seconds} segundos")
            self.log_event(0, channel.guild.id, channel.id, None, "channel_deleted", f"Canal de teste excluído após {seconds} segundos")
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            # Keep the failure in the audit trail if the bot lacks permission.
            try:
                self.log_event(0, channel.guild.id, channel.id, None, "channel_delete_failed", "Falha ao excluir canal; confira permissões do bot")
            except sqlite3.Error:
                pass


class CandidateAllView(discord.ui.View):
    def __init__(self, system, rows):
        super().__init__(timeout=180); self.system=system; self.rows=list(rows); self.page=0
        self.previous.disabled=True; self.next.disabled=len(self.rows)<=10
        self.copy_select=CandidateCopySelect(system,[int(r["user_id"]) for r in self.rows[:10]])
        self.add_item(self.copy_select)
    def refresh_copy_select(self):
        self.remove_item(self.copy_select)
        page_rows=self.rows[self.page*10:(self.page+1)*10]
        self.copy_select=CandidateCopySelect(self.system,[int(r["user_id"]) for r in page_rows])
        self.add_item(self.copy_select)
    def build_embed(self):
        labels={"pending":"⏳ Pendente","creating_test":"🛠️ Preparando teste","testing":"🎮 Em teste","approved":"💎 Aprovado","rejected":"❌ Reprovado"}
        page_rows=self.rows[self.page*10:(self.page+1)*10]
        embed=discord.Embed(title="📋 Todos os candidatos",description=f"Página {self.page+1} de {max(1,(len(self.rows)+9)//10)} · {len(self.rows)} cadastro(s)",color=discord.Color.blurple())
        for row in page_rows:
            embed.add_field(name=f"{discord.utils.escape_markdown(row['player_name'])} · {labels.get(row['status'],row['status'])}",value=f"Discord: <@{row['user_id']}> · UID: `{row['ff_id']}`\nWhatsApp: `{row['whatsapp'] or 'Não informado'}` · Cadastro: `{row['created_at']}`" + (f"\n**Etapa 2:** idade {row['stage_two_age'] or '—'} · função {row['stage_two_role'] or '—'} · experiência {row['stage_two_experience'] or '—'} · disponibilidade {row['stage_two_availability'] or '—'}" if row['status']=='approved' else ""),inline=False)
        return embed
    async def interaction_check(self, interaction):
        if not CandidateView(self.system).staff(interaction.user):
            await interaction.response.send_message("Somente ADM/equipe pode consultar candidatos.",ephemeral=True); return False
        return True
    @discord.ui.button(label="⬅️ Anterior",style=discord.ButtonStyle.secondary)
    async def previous(self,interaction,button):
        self.page=max(0,self.page-1); self.previous.disabled=self.page==0; self.next.disabled=(self.page+1)*10>=len(self.rows)
        self.refresh_copy_select()
        await interaction.response.edit_message(embed=self.build_embed(),view=self)
    @discord.ui.button(label="Próxima ➡️",style=discord.ButtonStyle.secondary)
    async def next(self,interaction,button):
        self.page=min((len(self.rows)-1)//10,self.page+1); self.previous.disabled=self.page==0; self.next.disabled=(self.page+1)*10>=len(self.rows)
        self.refresh_copy_select()
        await interaction.response.edit_message(embed=self.build_embed(),view=self)


class CandidateSearchModal(discord.ui.Modal, title="🔎 Pesquisar candidato"):
    query = discord.ui.TextInput(label="UID FF, WhatsApp, nick ou Discord", placeholder="Digite o ID, número ou nome", max_length=80)
    def __init__(self, system):
        super().__init__(timeout=180); self.system = system
    async def on_submit(self, interaction: discord.Interaction):
        query = str(self.query).strip().casefold()
        rows = list(self.system.search_applications(interaction.guild_id, str(self.query)))
        found_ids = {int(r["user_id"]) for r in rows}
        if interaction.guild:
            for member in interaction.guild.members:
                names = [member.name, member.display_name, getattr(member, "global_name", "") or ""]
                if any(query in str(name).casefold() for name in names if name):
                    candidate = self.system.get(member.id)
                    if candidate and candidate["guild_id"] == interaction.guild_id and member.id not in found_ids:
                        rows.append(candidate)
                        found_ids.add(member.id)
                        if len(rows) >= 10:
                            break
        if not rows:
            return await interaction.response.send_message("Nenhum candidato encontrado. Confira UID, WhatsApp, nick ou nome do Discord.", ephemeral=True)
        await interaction.response.send_message(embed=candidate_results_embed(rows), view=CandidateSearchResultsView(self.system, [int(r["user_id"]) for r in rows]), ephemeral=True)


def candidate_results_embed(rows):
    labels = {"pending":"⏳ Pendente", "testing":"🎮 Em teste", "approved":"💎 Aprovado", "rejected":"❌ Reprovado"}
    embed = discord.Embed(title="🔎 Resultados da pesquisa", description="Candidatos encontrados na base do servidor", color=discord.Color.from_rgb(130, 70, 220))
    for row in rows[:10]:
        embed.add_field(name=f"{labels.get(row['status'], row['status'])} · {discord.utils.escape_markdown(row['player_name'])}", value=f"**ID Free Fire:** `{row['ff_id']}`\n**WhatsApp:** `{row['whatsapp'] or 'Não informado'}`\n**Discord:** <@{row['user_id']}> (`{row['user_id']}`)" + (f"\n**Etapa 2:** idade {row['stage_two_age'] or '—'} · função {row['stage_two_role'] or '—'} · experiência {row['stage_two_experience'] or '—'} · disponibilidade {row['stage_two_availability'] or '—'}" if row['status']=='approved' else ""), inline=False)
    embed.set_footer(text="Os dados são privados e visíveis apenas para a equipe autorizada.")
    return embed


class CandidateCopySelect(discord.ui.Select):
    def __init__(self, system, user_ids):
        options=[]
        for uid in user_ids[:25]:
            row=system.get(uid)
            if row:
                options.append(discord.SelectOption(label=str(row["player_name"])[:100], value=str(uid), description=f"UID FF {row['ff_id']} · {row['status']}"[:100]))
        super().__init__(placeholder="Escolha um candidato para copiar os dados individuais", min_values=1, max_values=1, options=options)
        self.system=system
    async def callback(self, interaction):
        if not CandidateView(self.system).staff(interaction.user):
            return await interaction.response.send_message("Somente ADM/equipe pode copiar dados.", ephemeral=True)
        row=self.system.get(int(self.values[0]))
        if not row or row["guild_id"] != interaction.guild_id:
            return await interaction.response.send_message("Candidato não encontrado neste servidor.", ephemeral=True)
        lines=[f"Candidato: {row['player_name']}",f"Discord ID: {row['user_id']}",f"Free Fire UID: {row['ff_id']}",f"WhatsApp: {row['whatsapp'] or 'Não informado'}",f"Status: {row['status']}",f"Cadastro: {row['created_at']}"]
        if row["status"] == "approved":
            lines += [f"Idade: {row['stage_two_age'] or '—'}",f"Função: {row['stage_two_role'] or '—'}",f"Tempo de jogo: {row['stage_two_experience'] or '—'}",f"Disponibilidade: {row['stage_two_availability'] or '—'}"]
        safe_lines=[line.replace(chr(96), "'") for line in lines]
        await interaction.response.send_message("```text\n"+"\n".join(safe_lines)+"\n```", ephemeral=True)



class CandidateSearchResultsView(discord.ui.View):
    def __init__(self, system, user_ids):
        super().__init__(timeout=180); self.system = system
        if user_ids:
            self.add_item(CandidateCopySelect(system, user_ids))
        options = []
        for uid in user_ids[:25]:
            row = system.get(uid)
            if row:
                options.append(discord.SelectOption(label=str(row["player_name"])[:100], value=str(uid), description=f"UID FF {row['ff_id']} · {row['status']}"[:100]))
        if options:
            self.add_item(CandidateRemoveSelect(system, options))


class CandidateRemoveSelect(discord.ui.Select):
    def __init__(self, system, options):
        super().__init__(placeholder="Escolha um candidato para solicitar a remoção", min_values=1, max_values=1, options=options)
        self.system = system
    async def callback(self, interaction: discord.Interaction):
        if interaction.guild is None or not CandidateView(self.system).staff(interaction.user):
            return await interaction.response.send_message("Somente ADM/equipe pode remover candidatos.", ephemeral=True)
        user_id = int(self.values[0])
        row = self.system.get(user_id)
        if not row or row["guild_id"] != interaction.guild_id:
            return await interaction.response.send_message("Candidato não encontrado neste servidor.", ephemeral=True)
        view = CandidateRemoveConfirmView(self.system, user_id, interaction.user.id)
        await interaction.response.send_message(
            f"⚠️ Confirme a remoção do cadastro de **{discord.utils.escape_markdown(row['player_name'])}**.\n"
            f"Discord: <@{user_id}> · UID Free Fire: `{row['ff_id']}` · WhatsApp: `{row['whatsapp'] or 'Não informado'}`\n"
            "Esta ação remove o registro de candidatura e as respostas da segunda etapa. Não expulsa a pessoa nem apaga o canal automaticamente.",
            view=view, ephemeral=True
        )


class CandidateRemoveConfirmView(discord.ui.View):
    def __init__(self, system, user_id, staff_id):
        super().__init__(timeout=120)
        self.system = system
        self.user_id = int(user_id)
        self.staff_id = int(staff_id)

    async def interaction_check(self, interaction):
        if interaction.user.id != self.staff_id or not CandidateView(self.system).staff(interaction.user):
            await interaction.response.send_message("Somente o ADM que iniciou a confirmação pode concluir esta remoção.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Confirmar remoção", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction, button):
        row = self.system.remove_application(interaction.guild_id, self.user_id, interaction.user.id)
        if not row:
            return await interaction.response.edit_message(content="O cadastro já não existe ou não pôde ser removido.", view=None)
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(
            content=f"🗑️ Cadastro de **{discord.utils.escape_markdown(row['player_name'])}** removido.", view=self
        )

    @discord.ui.button(label="Cancelar", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction, button):
        await interaction.response.edit_message(content="Remoção cancelada. Nenhum dado foi alterado.", view=None)
