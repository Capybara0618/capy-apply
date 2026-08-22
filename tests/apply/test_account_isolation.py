from capybot.apply.store import ApplyStore


def _seed_account(store: ApplyStore, account_id: str, text: str) -> tuple[str, str]:
    store.upsert_account({"id": account_id, "account_uid": account_id, "display_name": account_id})
    conversation_id = store.upsert_conversation(
        {
            "account_id": account_id,
            "conversation_id": "same-platform-conversation",
            "boss_uid": "same-boss",
            "contact_name": "Same HR",
        }
    )
    store.upsert_message(
        {
            "conversation_id": conversation_id,
            "message_id": "same-platform-message",
            "from_me": False,
            "text": text,
            "message_type": "text",
        }
    )
    opportunity_id = store.ensure_opportunities_for_conversation(conversation_id)[0]
    return conversation_id, opportunity_id


def test_accounts_can_store_same_platform_ids_without_cross_reading() -> None:
    store = ApplyStore()
    _, opportunity_a = _seed_account(store, "account-a", "account A evidence")
    _, opportunity_b = _seed_account(store, "account-b", "account B evidence")

    assert opportunity_a != opportunity_b
    assert [row["id"] for row in store.opportunities()] == [opportunity_b]
    assert [
        row["text"] for row in store.message_evidence(["same-platform-message"])["messages"]
    ] == ["account B evidence"]

    store.upsert_account(
        {"id": "account-a", "account_uid": "account-a", "display_name": "account-a"}
    )

    assert [row["id"] for row in store.opportunities()] == [opportunity_a]
    assert [
        row["text"] for row in store.message_evidence(["same-platform-message"])["messages"]
    ] == ["account A evidence"]


def test_explicit_account_scope_does_not_follow_latest_account() -> None:
    account_a = ApplyStore(account_id="account-a")
    account_b = ApplyStore(account_id="account-b")
    _seed_account(account_a, "account-a", "account A evidence")
    _seed_account(account_b, "account-b", "account B evidence")

    # Touching another account must not change the account captured by a Worker.
    account_b.upsert_account(
        {"id": "account-b", "account_uid": "account-b", "display_name": "account-b"}
    )

    assert account_a.current_account_id() == "account-a"
    assert [
        row["text"] for row in account_a.message_evidence(["same-platform-message"])["messages"]
    ] == ["account A evidence"]


def test_subprocess_store_can_bind_account_from_opportunity() -> None:
    account_a = ApplyStore(account_id="account-a")
    account_b = ApplyStore(account_id="account-b")
    _, opportunity_a = _seed_account(account_a, "account-a", "account A evidence")
    _seed_account(account_b, "account-b", "account B evidence")

    subprocess_store = ApplyStore()
    assert subprocess_store.bind_opportunity_account(opportunity_a) == "account-a"
    context = subprocess_store.opportunity_context(opportunity_a)

    assert context["opportunity"]["account_id"] == "account-a"
    assert [row["text"] for row in context["messages"]] == ["account A evidence"]


def test_profile_and_preferences_are_scoped_per_account() -> None:
    account_a = ApplyStore(account_id="account-a")
    account_b = ApplyStore(account_id="account-b")
    account_a.upsert_account(
        {"id": "account-a", "account_uid": "account-a", "display_name": "account-a"}
    )
    account_b.upsert_account(
        {"id": "account-b", "account_uid": "account-b", "display_name": "account-b"}
    )

    account_a.update_profile(
        {
            "resume_markdown": "# Account A\nPython Agent",
            "preferences": {"target_roles": "Agent intern", "cities": "Hangzhou"},
        }
    )
    account_b.update_profile(
        {
            "resume_markdown": "# Account B\nReact frontend",
            "preferences": {"target_roles": "Frontend intern", "cities": "Shanghai"},
        }
    )

    assert account_a.profile_payload()["profile"]["resume_markdown"].startswith("# Account A")
    assert account_a.profile_payload()["preferences"]["target_roles"] == "Agent intern"
    assert account_b.profile_payload()["profile"]["resume_markdown"].startswith("# Account B")
    assert account_b.profile_payload()["preferences"]["target_roles"] == "Frontend intern"


def test_real_boss_uid_adopts_existing_profile_placeholder_account() -> None:
    store = ApplyStore()
    profile_dir = r"C:\Users\tester\.capybot\browser\boss-profile"
    placeholder_id = store.upsert_account(
        {
            "id": "boss_profile_placeholder",
            "account_uid": "profile:placeholder",
            "profile_dir": profile_dir,
            "display_name": "BOSS 本地账号",
        }
    )
    conversation_id = store.upsert_conversation(
        {
            "account_id": placeholder_id,
            "conversation_id": "conv-before-identity",
            "boss_uid": "boss-1",
            "contact_name": "测试 HR",
        }
    )

    resolved_id = store.upsert_account(
        {
            "id": "boss_account_real",
            "account_uid": "660012954",
            "profile_dir": profile_dir,
            "display_name": "真实账号",
        }
    )

    assert resolved_id == placeholder_id
    assert store.current_account()["account_uid"] == "660012954"
    with store.connect() as db:
        assert db.execute(
            "SELECT COUNT(*) FROM boss_conversations WHERE id=? AND account_id=?",
            (conversation_id, placeholder_id),
        ).fetchone()[0] == 1
