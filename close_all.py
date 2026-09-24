# close_all.py
import os
import time
import smtplib
import pytz
from email.mime.text import MIMEText
from email.header import Header
from datetime import datetime

from alpaca.trading.client import TradingClient
from config import *

ET = pytz.timezone("America/New_York")


def send_email(subject, body):
    """发送邮件通知"""
    mail_user = os.getenv("MAIL_USERNAME")
    mail_pass = os.getenv("MAIL_PASSWORD")
    if not mail_user or not mail_pass:
        print("⚠️ 未配置邮箱，跳过邮件发送")
        return

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = mail_user
    msg["To"] = mail_user

    try:
        server = smtplib.SMTP_SSL("smtp.gmail.com", 465)
        server.login(mail_user, mail_pass)
        server.sendmail(mail_user, [mail_user], msg.as_string())
        server.quit()
        print("✅ 邮件通知已发送")
    except Exception as e:
        print(f"⚠️ 邮件发送失败: {e}")


def main():
    print("=" * 60)
    print(f"  强制平仓 | {datetime.now(ET).strftime('%Y-%m-%d %H:%M:%S ET')}")
    print("=" * 60)

    trading_client = TradingClient(
        ALPACA_API_KEY, ALPACA_SECRET_KEY, paper=ALPACA_PAPER
    )

    try:
        positions = trading_client.get_all_positions()
    except Exception as e:
        send_email("ORB策略-获取持仓失败", f"获取持仓失败: {e}")
        return

    if not positions:
        print("无持仓，无需平仓")
        return

    errors = []
    for p in positions:
        symbol = p.symbol
        print(f"\n── {symbol} ──")

        try:
            open_orders = trading_client.get_orders(
                status="open", symbols=[symbol]
            )
            for o in open_orders:
                try:
                    trading_client.cancel_order_by_id(o.id)
                    print(f"  🗑️ 已取消订单: {o.id}")
                except Exception as e:
                    print(f"  ⚠️ 取消失败: {e}")
        except Exception as e:
            print(f"  ⚠️ 查询未成交订单失败: {e}")

        time.sleep(1.0)

        try:
            trading_client.close_position(symbol)
            print(f"  ⏰ 平仓: {symbol}")
        except Exception as e:
            errors.append(f"{symbol} 平仓失败: {e}")
            print(f"  ⚠️ 平仓失败: {e}")

    if errors:
        send_email("ORB策略-平仓异常", "\n".join(errors))
    else:
        send_email("ORB策略-平仓完成", f"已成功平仓所有持仓，时间: {datetime.now(ET)}")

    print("\n平仓完成")


if __name__ == "__main__":
    main()