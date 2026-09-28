import asyncio
import logging
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram import Update
from telegram.error import BadRequest

from lottery.bot import (
    BotHandlers,
    Settings,
    TokenFilter,
    build_application,
    card,
    main,
    on_error,
    personal_weight,
    result_text,
)
from lottery.core import LotteryError, Store

TOKEN = "123456:SECRET-token"
USER = {"id": 123, "is_bot": False, "first_name": "Alice"}
GROUP = {"id": -100, "type": "supergroup"}


@pytest.fixture
def setup(tmp_path):
    store = Store(tmp_path / "test.sqlite3")
    rid = store.create(99, "测试抽奖", 2, 60)
    return store, rid, BotHandlers(store, {99})


def command_update(text, user_id=99, chat_type="private"):
    message = SimpleNamespace(
        text=text, sender_chat=None, reply_text=AsyncMock(), reply_document=AsyncMock()
    )
    update = SimpleNamespace(
        effective_message=message,
        effective_user=SimpleNamespace(id=user_id, is_bot=False),
        effective_chat=SimpleNamespace(type=chat_type),
    )
    return update, SimpleNamespace(args=text.split()[1:])


def run_command(handlers, text, user_id=99, chat_type="private"):
    update, context = command_update(text, user_id, chat_type)
    asyncio.run(handlers.command(update, context))
    return update.effective_message


def test_non_admin_cannot_change_weights_or_draw(setup):
    store, rid, handlers = setup
    for text in [f"/weight {rid} 123 10", f"/freeze {rid}", f"/draw {rid}", f"/export {rid}"]:
        message = run_command(handlers, text, user_id=123)
        assert "仅限" in message.reply_text.call_args.args[0]
    assert store.view(rid)["status"] == "OPEN"
    assert len(store.export(rid)["audit"]) == 1


def test_sensitive_command_requires_private_chat(setup):
    store, rid, handlers = setup
    message = run_command(handlers, f"/weight {rid} 123 10", chat_type="supergroup")
    assert "私聊" in message.reply_text.call_args.args[0]
    assert len(store.export(rid)["audit"]) == 1


def test_admin_flow_and_group_draw(setup):
    store, rid, handlers = setup
    run_command(handlers, f"/rule {rid} vip 2")
    run_command(handlers, f"/grant {rid} 123 vip")
    store.join(rid, 123, "Alice")
    assert store.view(rid)["entries"][0]["weight"] == 3
    message = run_command(handlers, f"/publish {rid}", chat_type="supergroup")
    assert message.reply_text.call_args.kwargs["reply_markup"] is not None
    run_command(handlers, f"/freeze {rid}")
    first = run_command(handlers, f"/draw {rid}", chat_type="supergroup")
    second = run_command(handlers, f"/draw {rid}", chat_type="supergroup")
    assert first.reply_text.call_args == second.reply_text.call_args
    assert "Alice" in first.reply_text.call_args.args[0]


def test_bad_arguments_do_not_modify_data(setup):
    store, rid, handlers = setup
    for text in [
        f"/weight {rid} 123",
        f"/weight {rid} 123 -1",
        f"/weight {rid} 123 1.5",
        f"/rule {rid} bad/tag 2",
        "/draw 9999999999999999999999999",
    ]:
        assert run_command(handlers, text).reply_text.await_count
    assert len(store.export(rid)["audit"]) == 1


def test_callback_registration_and_deadline(setup):
    store, rid, handlers = setup
    query = SimpleNamespace(
        data=f"join:{rid}",
        answer=AsyncMock(),
        from_user=SimpleNamespace(id=123, full_name="Alice", is_bot=False),
    )
    update = SimpleNamespace(callback_query=query)
    asyncio.run(handlers.callback(update, None))
    assert "成功" in query.answer.call_args.args[0]
    asyncio.run(handlers.callback(update, None))
    assert "无需重复" in query.answer.call_args.args[0]
    store.freeze(rid, 99)
    asyncio.run(handlers.callback(update, None))
    assert "冻结" in query.answer.call_args.args[0]


def test_personal_weight_zero_and_result_display(setup):
    store, rid, _ = setup
    store.join(rid, 123, "Alice")
    store.override(rid, 99, 123, 0)
    assert "0.00%" in personal_weight(store.view(rid), 123)
    store.freeze(rid, 99)
    text, markup = card(store.view(rid))
    assert "已冻结" in text
    assert all(
        not b.callback_data.startswith("join:") for row in markup.inline_keyboard for b in row
    )
    assert "实际抽出 0 名" in result_text(store.draw(rid, 99))


def test_build_application_offline(tmp_path, monkeypatch):
    # httpx reads proxy variables; an ambient SOCKS proxy would need the optional socksio.
    for name in ("ALL_PROXY", "HTTPS_PROXY", "HTTP_PROXY"):
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(name.lower(), raising=False)
    app = build_application(
        Settings("123456:offline-test-token", frozenset({99}), str(tmp_path / "test.sqlite3"))
    )
    assert len(app.handlers[0]) == 2
    assert app.concurrent_updates == 1


def test_missing_configuration_fails_closed(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    with pytest.raises(LotteryError, match="TELEGRAM_BOT_TOKEN"):
        Settings.from_env()
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:test")
    monkeypatch.setenv("ADMIN_USER_IDS", "")
    with pytest.raises(LotteryError, match="ADMIN_USER_IDS"):
        Settings.from_env()


def test_delivery_failure_does_not_reroll(setup):
    store, rid, handlers = setup
    store.join(rid, 123, "Alice")
    store.freeze(rid, 99)
    update, context = command_update(f"/draw {rid}")
    update.effective_message.reply_text.side_effect = RuntimeError("network down")
    with pytest.raises(RuntimeError):
        asyncio.run(handlers.command(update, context))
    saved = store.view(rid)["result"]
    run_command(handlers, f"/draw {rid}")
    assert store.view(rid)["result"] == saved


@pytest.mark.parametrize(
    "text",
    [
        "/new 2 60 test",
        "/config 1 1 100",
        "/rule 1 vip 2",
        "/grant 1 123 vip",
        "/revoke 1 123 vip",
        "/weight 1 123 10",
        "/preview 1",
        "/export 1",
        "/freeze 1",
        "/raffles",
        "/myweight 1",
    ],
)
def test_private_operations_never_reply_with_sensitive_data_in_group(setup, text):
    store, _, handlers = setup
    message = run_command(handlers, text, chat_type="supergroup")
    assert message.reply_text.call_args.args[0] == "请私聊机器人执行此命令。"
    message.reply_document.assert_not_awaited()
    assert len(store.recent()) == 1
    assert len(store.export(1)["audit"]) == 1


def test_weight_callback_only_displays_personal_popup(setup):
    store, rid, handlers = setup
    store.join(rid, 123, "Alice")
    store.override(rid, 99, 123, 10)
    query = SimpleNamespace(
        data=f"weight:{rid}", answer=AsyncMock(), from_user=SimpleNamespace(id=123, is_bot=False)
    )
    update = SimpleNamespace(callback_query=query)
    asyncio.run(handlers.callback(update, None))
    assert "权重：10" in query.answer.call_args.args[0]
    assert query.answer.call_args.kwargs == {"show_alert": True}


def test_anonymous_sender_cannot_execute_management_command(setup):
    store, rid, handlers = setup
    update, context = command_update(f"/freeze {rid}")
    update.effective_message.sender_chat = SimpleNamespace(id=-1001)
    asyncio.run(handlers.command(update, context))
    assert store.view(rid)["status"] == "OPEN"


def test_freeze_after_draw_reports_saved_result(setup):
    store, rid, handlers = setup
    store.freeze(rid, 99)
    store.draw(rid, 99)
    message = run_command(handlers, f"/freeze {rid}")
    assert "已开奖" in message.reply_text.call_args.args[0]


def test_stale_button_answer_failure_keeps_join_and_stays_silent(setup):
    store, rid, handlers = setup
    query = SimpleNamespace(
        data=f"join:{rid}",
        answer=AsyncMock(side_effect=BadRequest("Query is too old")),
        from_user=SimpleNamespace(id=123, full_name="Alice", is_bot=False),
    )
    asyncio.run(handlers.callback(SimpleNamespace(callback_query=query), None))
    assert [p["user_id"] for p in store.view(rid)["entries"]] == [123]


def telegram_update(payload):
    bot = SimpleNamespace(answer_callback_query=AsyncMock(), send_message=AsyncMock())
    update = Update.de_json({"update_id": 1, **payload}, None)
    for obj in (update.callback_query, update.effective_message):
        if obj is not None:
            obj.set_bot(bot)
    return update, bot


def test_button_error_is_answered_privately_not_in_group():
    card_message = {"message_id": 5, "date": 0, "chat": GROUP, "text": "card"}
    update, bot = telegram_update(
        {
            "callback_query": {
                "id": "q",
                "chat_instance": "c",
                "data": "join:1",
                "from": USER,
                "message": card_message,
            }
        }
    )
    asyncio.run(on_error(update, SimpleNamespace(error=RuntimeError("database is locked"))))
    bot.send_message.assert_not_awaited()
    assert bot.answer_callback_query.await_args.kwargs["show_alert"] is True


def test_command_error_still_replies_in_chat():
    message = {"message_id": 5, "date": 0, "chat": GROUP, "from": USER, "text": "/draw 1"}
    update, bot = telegram_update({"message": message})
    asyncio.run(on_error(update, SimpleNamespace(error=RuntimeError("network down"))))
    assert "请重试" in bot.send_message.await_args.kwargs["text"]


def test_token_filter_masks_message_and_traceback():
    record = logging.LogRecord(
        "t", logging.ERROR, __file__, 1, "GET %s", (f"https://x/bot{TOKEN}/getMe",), None
    )
    try:
        raise RuntimeError(f"failed for {TOKEN}")
    except RuntimeError:
        record.exc_info = sys.exc_info()
    TokenFilter(TOKEN).filter(record)
    text = logging.Formatter().format(record)
    assert "SECRET" not in text
    assert "bot123456:***/getMe" in text
    assert "RuntimeError: failed for 123456:***" in text


def test_startup_failure_keeps_cause_but_masks_token(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("ADMIN_USER_IDS", "99")
    monkeypatch.setattr(logging.root, "handlers", [])  # main() installs its own handler

    def broken(settings):
        raise RuntimeError(f'install "python-telegram-bot[socks]" ({settings.token})')

    monkeypatch.setattr("lottery.bot.build_application", broken)
    with pytest.raises(SystemExit) as exit_info:
        main()
    message = str(exit_info.value)
    assert 'RuntimeError: install "python-telegram-bot[socks]"' in message
    assert "SECRET" not in message
