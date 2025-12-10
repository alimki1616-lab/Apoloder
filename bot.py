import logging
import asyncio
import os
from datetime import datetime, timezone, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, CallbackQueryHandler
from dotenv import load_dotenv
import secrets

load_dotenv()

# Configure logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Bot configuration
BOT_TOKEN = os.environ.get('BOT_TOKEN')
MAIN_ADMIN_ID = int(os.environ.get('MAIN_ADMIN_ID', '0'))
FILE_DELETE_SECONDS = 15

class TelegramBot:
    def __init__(self):
        self.application = Application.builder().token(BOT_TOKEN).build()
        self.bot = self.application.bot
        
        # In-memory storage (instead of MongoDB)
        self.users = {}  # user_id -> user_info
        self.admins = {MAIN_ADMIN_ID: {'username': 'main_admin', 'added_at': datetime.now(timezone.utc).isoformat()}}
        self.files = {}  # unique_code -> file_info
        self.mandatory_channels = {}  # channel_id -> channel_info
        self.spam_control = {}  # user_id -> spam_info
        self.user_message_map = {}  # message_id -> user_id (for admin replies)
        self.downloads = []  # list of download records
        
    def is_admin(self, user_id: int) -> bool:
        """Check if user is admin"""
        return user_id in self.admins
    
    async def check_membership(self, user_id: int) -> tuple[bool, list]:
        """Check if user is member of all mandatory channels"""
        if not self.mandatory_channels:
            return True, []
        
        not_joined = []
        for channel_id, channel_info in self.mandatory_channels.items():
            try:
                member = await self.bot.get_chat_member(
                    chat_id=channel_id,
                    user_id=user_id
                )
                if member.status not in ['member', 'administrator', 'creator']:
                    not_joined.append(channel_info)
            except Exception as e:
                logger.error(f"Error checking membership for channel {channel_id}: {e}")
                not_joined.append(channel_info)
        
        return len(not_joined) == 0, not_joined
    
    async def schedule_message_deletion_and_send_buttons(self, chat_id: int, message_id: int, delay_seconds: int, file_code: str = None):
        """Delete a message after specified seconds and send buttons"""
        await asyncio.sleep(delay_seconds)
        
        try:
            await self.bot.delete_message(chat_id=chat_id, message_id=message_id)
            logger.info(f"Message {message_id} deleted from chat {chat_id} after {delay_seconds} seconds")
            
            # Send buttons after deletion
            keyboard = []
            if file_code:
                keyboard.append([InlineKeyboardButton("🔄 دریافت مجدد محتوا", callback_data=f"redownload_{file_code}")])
            keyboard.append([InlineKeyboardButton("📞 ارتباط با مدیر", callback_data="contact_admin")])
            
            await self.bot.send_message(
                chat_id=chat_id,
                text="محتوا پاک شد. می‌توانید دوباره دریافت کنید یا با مدیر ارتباط برقرار کنید:",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        except Exception as e:
            logger.error(f"Error in deletion process {message_id}: {e}")

    def get_admin_keyboard(self):
        """Create admin menu keyboard"""
        keyboard = [
            [InlineKeyboardButton("👥 لیست کاربران فعال", callback_data="users")],
            [InlineKeyboardButton("🚫 کاربران بلاک شده", callback_data="blocked")],
            [InlineKeyboardButton("🔨 بلاک کردن کاربر", callback_data="block_user")],
            [InlineKeyboardButton("📢 ارسال پیام به همه", callback_data="broadcast"),
             InlineKeyboardButton("📩 پیام به کاربر خاص", callback_data="send_to_user")],
            [InlineKeyboardButton("📢 کانال‌های اجباری", callback_data="channels")],
            [InlineKeyboardButton("➕ افزودن کانال", callback_data="add_channel"),
             InlineKeyboardButton("➖ حذف کانال", callback_data="remove_channel")],
            [InlineKeyboardButton("👤 افزودن ادمین", callback_data="add_admin"),
             InlineKeyboardButton("❌ حذف ادمین", callback_data="remove_admin")],
        ]
        return InlineKeyboardMarkup(keyboard)
    
    def get_user_keyboard(self, file_code: str = None):
        """Create user menu keyboard with contact admin button"""
        keyboard = []
        
        if file_code:
            keyboard.append([InlineKeyboardButton("🔄 دریافت مجدد محتوا", callback_data=f"redownload_{file_code}")])
        
        keyboard.append([InlineKeyboardButton("📞 ارتباط با مدیر", callback_data="contact_admin")])
        
        return InlineKeyboardMarkup(keyboard)
    
    def check_spam(self, user_id: int) -> tuple[bool, int]:
        """Check if user is spamming"""
        now = datetime.now(timezone.utc)
        
        if user_id in self.spam_control:
            spam_info = self.spam_control[user_id]
            last_request = datetime.fromisoformat(spam_info['last_request'])
            time_diff = (now - last_request).total_seconds()
            
            if time_diff < 5:
                request_count = spam_info.get('request_count', 0) + 1
                
                self.spam_control[user_id] = {
                    'request_count': request_count,
                    'last_request': now.isoformat(),
                    'blocked_until': (now + timedelta(seconds=30)).isoformat() if request_count >= 3 else None
                }
                
                if request_count >= 3:
                    return True, 30
                
                return True, int(5 - time_diff)
            else:
                self.spam_control[user_id] = {
                    'request_count': 1,
                    'last_request': now.isoformat()
                }
        else:
            self.spam_control[user_id] = {
                'request_count': 1,
                'last_request': now.isoformat()
            }
        
        return False, 0
    
    def is_temp_blocked(self, user_id: int) -> tuple[bool, int]:
        """Check if user is temporarily blocked"""
        if user_id in self.spam_control and self.spam_control[user_id].get('blocked_until'):
            blocked_until = datetime.fromisoformat(self.spam_control[user_id]['blocked_until'])
            now = datetime.now(timezone.utc)
            
            if now < blocked_until:
                remaining = int((blocked_until - now).total_seconds())
                return True, remaining
            else:
                self.spam_control[user_id].pop('blocked_until', None)
        
        return False, 0
    
    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /start command"""
        user = update.effective_user

        # Check if user is blocked
        if user.id in self.users and self.users[user.id].get('is_blocked', False):
            await update.message.reply_text(
                "⛔ شما توسط ادمین بلاک شده‌اید.\n\n"
                "برای رفع مسدودیت با ادمین تماس بگیرید."
            )
            return
        
        # Update or create user
        self.users[user.id] = {
            'user_id': user.id,
            'username': user.username or 'unknown',
            'first_name': user.first_name or 'unknown',
            'is_blocked': False,
            'last_seen': datetime.now(timezone.utc).isoformat()
        }
        
        is_admin = self.is_admin(user.id)
        
        # Check if this is a file access request
        if context.args and len(context.args) > 0:
            file_code = context.args[0]
            await self.handle_file_access(update, context, file_code)
            return
        
        # Regular start message
        if is_admin:
            await update.message.reply_text(
                f"👋 سلام {user.first_name}!\n\n"
                f"✨ شما ادمین هستید. برای آپلود فایل، عکس یا ویدیو را ارسال کنید.\n\n"
                f"📝 بات ابتدا فایل را دریافت می‌کند و سپس از شما متن پست را می‌خواهد.\n\n"
                f"💬 برای پاسخ به پیام کاربران، روی پیام آن‌ها Reply کنید.\n\n"
                f"⚠️ توجه: بات بدون دیتابیس است. با restart، لینک‌ها و تنظیمات پاک می‌شوند!\n\n"
                f"از دکمه‌های زیر برای مدیریت بات استفاده کنید:",
                reply_markup=self.get_admin_keyboard()
            )
        else:
            await update.message.reply_text(
                f"👋 سلام {user.first_name}!\n\n"
                f"برای دریافت فایل‌ها، لینک را از ادمین دریافت کنید.\n\n"
                f"یا می‌توانید از دکمه زیر برای ارتباط با مدیر استفاده کنید:",
                reply_markup=self.get_user_keyboard()
            )
    
    async def handle_file_access(self, update: Update, context: ContextTypes.DEFAULT_TYPE, file_code: str):
        """Handle file access request"""
        user = update.effective_user
        
        # Check temporary spam block
        is_blocked, remaining = self.is_temp_blocked(user.id)
        if is_blocked:
            await update.message.reply_text(
                f"⛔ شما به دلیل درخواست‌های مکرر به صورت موقت مسدود شده‌اید.\n\n"
                f"⏱️ زمان باقی‌مانده: {remaining} ثانیه"
            )
            return
        
        # Check spam
        is_spam, wait_time = self.check_spam(user.id)
        if is_spam:
            if wait_time >= 30:
                await update.message.reply_text(
                    f"⛔ شما به دلیل اسپم برای 30 ثانیه مسدود شدید!\n\n"
                    f"لطفاً صبر کنید."
                )
            else:
                await update.message.reply_text(
                    f"⚠️ لطفاً کمی صبر کنید.\n\n"
                    f"⏱️ {wait_time} ثانیه دیگر تلاش کنید."
                )
            return
        
        # Check if file exists
        if file_code not in self.files:
            await update.message.reply_text("❌ این لینک وجود ندارد یا منقضی شده است.")
            return
        
        # Check membership
        is_member, not_joined_channels = await self.check_membership(user.id)
        
        if not is_member:
            keyboard = []
            for channel in not_joined_channels:
                keyboard.append([InlineKeyboardButton(
                    f"عضویت در {channel['channel_username']}",
                    url=f"https://t.me/{channel['channel_username'].replace('@', '')}"
                )])
            keyboard.append([InlineKeyboardButton(
                "✅ عضو شدم",
                callback_data=f"check_{file_code}"
            )])
            
            reply_markup = InlineKeyboardMarkup(keyboard)
            await update.message.reply_text(
                "⚠️ برای دریافت فایل، ابتدا باید در کانال‌ها/گروه‌های زیر عضو شوید:",
                reply_markup=reply_markup
            )
            return
        
        # Send file
        await self.send_file_to_user(user.id, self.files[file_code], file_code)
    
    async def send_file_to_user(self, user_id: int, file_doc: dict, file_code: str):
        """Send file to user"""
        try:
            user_caption = file_doc.get('caption', '')
            if user_caption:
                full_caption = f"{user_caption}\n\n⏱️ این محتوا بعد از {FILE_DELETE_SECONDS} ثانیه پاک می‌شود!"
            else:
                full_caption = f"⏱️ این محتوا بعد از {FILE_DELETE_SECONDS} ثانیه پاک می‌شود!"
            
            sent_message = None
            
            if file_doc['file_type'] == 'photo':
                sent_message = await self.bot.send_photo(
                    chat_id=user_id,
                    photo=file_doc['telegram_file_id'],
                    caption=full_caption
                )
            elif file_doc['file_type'] == 'video':
                sent_message = await self.bot.send_video(
                    chat_id=user_id,
                    video=file_doc['telegram_file_id'],
                    caption=full_caption
                )
            
            if sent_message:
                asyncio.create_task(
                    self.schedule_message_deletion_and_send_buttons(
                        chat_id=user_id,
                        message_id=sent_message.message_id,
                        delay_seconds=FILE_DELETE_SECONDS,
                        file_code=file_code
                    )
                )
            
            # Track download
            self.downloads.append({
                'file_code': file_code,
                'user_id': user_id,
                'downloaded_at': datetime.now(timezone.utc).isoformat()
            })
            
            logger.info(f"File {file_code} sent to user {user_id}")
        except Exception as e:
            logger.error(f"Error sending file: {e}")
            await self.bot.send_message(
                chat_id=user_id,
                text="❌ خطا در ارسال فایل."
            )
    
    async def handle_media(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle photo/video uploads"""
        user = update.effective_user

        if self.is_admin(user.id):
            await self.handle_admin_media(update, context)
        else:
            if context.user_data.get('awaiting') == 'user_content_to_admin':
                await self.handle_user_media_to_admin(update, context)
            else:
                await update.message.reply_text(
                    "❌ لطفاً ابتدا از دکمه «ارتباط با مدیر» استفاده کنید.",
                    reply_markup=self.get_user_keyboard()
                )
    
    async def handle_admin_media(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle admin file upload"""
        file_type = None
        telegram_file_id = None
        
        if update.message.photo:
            file_type = 'photo'
            telegram_file_id = update.message.photo[-1].file_id
        elif update.message.video:
            file_type = 'video'
            telegram_file_id = update.message.video.file_id
        else:
            await update.message.reply_text("❌ فقط عکس و ویدیو پشتیبانی می‌شود.")
            return
        
        context.user_data['awaiting'] = 'caption_for_file'
        context.user_data['temp_file'] = {
            'file_type': file_type,
            'telegram_file_id': telegram_file_id
        }
        
        keyboard = [[InlineKeyboardButton("🚫 بدون متن", callback_data="no_caption")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await update.message.reply_text(
            "✅ فایل دریافت شد!\n\n"
            "📝 لطفاً متن پست را ارسال کنید:\n\n"
            "یا روی دکمه «بدون متن» کلیک کنید.",
            reply_markup=reply_markup
        )
    
    async def handle_user_media_to_admin(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle user sending media to admin"""
        file_type = None
        telegram_file_id = None
        
        if update.message.video:
            file_type = 'video'
            telegram_file_id = update.message.video.file_id
        elif update.message.photo:
            file_type = 'photo'
            telegram_file_id = update.message.photo[-1].file_id
        else:
            await update.message.reply_text("❌ لطفاً یک عکس یا ویدیو ارسال کنید.")
            return
        
        context.user_data['temp_user_file'] = {
            'file_type': file_type,
            'telegram_file_id': telegram_file_id
        }
        context.user_data['awaiting'] = 'user_caption_to_admin'
        
        keyboard = [[InlineKeyboardButton("🚫 بدون توضیحات", callback_data="no_user_caption")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await update.message.reply_text(
            "✅ فایل دریافت شد!\n\n"
            "📝 لطفاً توضیحات خود را ارسال کنید:\n\n"
            "یا روی دکمه «بدون توضیحات» کلیک کنید.",
            reply_markup=reply_markup
        )
    
    async def forward_to_admins(self, message_type: str, content: str, user_info: dict, telegram_file_id: str = None):
        """Forward user's message to all admins"""
        header_text = (
            f"📩 پیام جدید از کاربر:\n\n"
            f"👤 نام: {user_info.get('first_name', 'Unknown')}\n"
            f"🆔 آیدی: {user_info['user_id']}\n"
            f"👤 یوزرنیم: @{user_info.get('username', 'ندارد')}\n\n"
        )
        
        for admin_id in self.admins.keys():
            try:
                sent_msg = None
                
                if message_type == 'text':
                    full_text = f"{header_text}💬 پیام:\n{content}\n\n💡 برای پاسخ، روی این پیام Reply کنید."
                    sent_msg = await self.bot.send_message(
                        chat_id=admin_id,
                        text=full_text
                    )
                elif message_type == 'photo':
                    caption = f"{header_text}💬 توضیحات:\n{content if content else 'بدون توضیحات'}\n\n💡 برای پاسخ، روی این پیام Reply کنید."
                    sent_msg = await self.bot.send_photo(
                        chat_id=admin_id,
                        photo=telegram_file_id,
                        caption=caption
                    )
                elif message_type == 'video':
                    caption = f"{header_text}💬 توضیحات:\n{content if content else 'بدون توضیحات'}\n\n💡 برای پاسخ، روی این پیام Reply کنید."
                    sent_msg = await self.bot.send_video(
                        chat_id=admin_id,
                        video=telegram_file_id,
                        caption=caption
                    )
                
                if sent_msg:
                    self.user_message_map[sent_msg.message_id] = user_info['user_id']
                    
                logger.info(f"User message forwarded to admin {admin_id}")
            except Exception as e:
                logger.error(f"Error forwarding to admin {admin_id}: {e}")
    
    async def handle_admin_reply(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle admin reply to user message"""
        if not update.message.reply_to_message:
            return False
        
        user = update.effective_user
        
        if not self.is_admin(user.id):
            return False
        
        replied_to_message_id = update.message.reply_to_message.message_id
        target_user_id = self.user_message_map.get(replied_to_message_id)
        
        if not target_user_id:
            return False
        
        try:
            reply_text = f"💬 پاسخ از ادمین:\n\n{update.message.text}"
            await self.bot.send_message(
                chat_id=target_user_id,
                text=reply_text
            )
            await update.message.reply_text("✅ پیام شما به کاربر ارسال شد.")
            logger.info(f"Admin {user.id} replied to user {target_user_id}")
            return True
        except Exception as e:
            logger.error(f"Error sending admin reply: {e}")
            await update.message.reply_text("❌ خطا در ارسال پیام به کاربر.")
            return True
    
    async def broadcast_message(self, message_text: str, admin_id: int):
        """Send message to all active users"""
        success_count = 0
        fail_count = 0
        
        for user_id, user_info in self.users.items():
            if user_info.get('is_blocked', False):
                continue
                
            try:
                await self.bot.send_message(
                    chat_id=user_id,
                    text=message_text
                )
                success_count += 1
                await asyncio.sleep(0.05)
            except Exception as e:
                logger.error(f"Error broadcasting to user {user_id}: {e}")
                fail_count += 1
        
        await self.bot.send_message(
            chat_id=admin_id,
            text=f"📊 گزارش ارسال پیام:\n\n✅ موفق: {success_count}\n❌ ناموفق: {fail_count}"
        )
    
    async def button_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle button callbacks"""
        query = update.callback_query
        await query.answer()
        
        user = update.effective_user
        data = query.data
        
        # Check admin permission
        if data in ['users', 'blocked', 'channels', 'add_channel', 'remove_channel', 'add_admin', 'remove_admin', 'block_user', 'broadcast', 'send_to_user']:
            if not self.is_admin(user.id):
                await query.edit_message_text("❌ فقط ادمین‌ها دسترسی دارند.")
                return
        
        # Handle user actions
        if data == "contact_admin":
            context.user_data['awaiting'] = 'user_content_to_admin'
            keyboard = [[InlineKeyboardButton("❌ لغو", callback_data="cancel_user_send")]]
            await query.edit_message_text(
                "📞 ارتباط با مدیر\n\n"
                "لطفاً پیام، عکس یا ویدیوی خود را ارسال کنید:",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
            return
        
        elif data == "cancel_user_send":
            context.user_data.clear()
            await query.edit_message_text(
                f"👋 سلام {user.first_name}!\n\n"
                f"برای دریافت فایل‌ها، لینک را از ادمین دریافت کنید.\n\n"
                f"یا می‌توانید از دکمه زیر برای ارتباط با مدیر استفاده کنید:",
                reply_markup=self.get_user_keyboard()
            )
            return
        
        elif data == "no_user_caption":
            if 'temp_user_file' not in context.user_data:
                await query.edit_message_text("❌ خطا: فایلی یافت نشد. لطفاً دوباره تلاش کنید.")
                context.user_data.clear()
                return
            
            temp_file = context.user_data['temp_user_file']
            user_info = {
                'user_id': user.id,
                'username': user.username,
                'first_name': user.first_name
            }
            
            await self.forward_to_admins(
                message_type=temp_file['file_type'],
                content=None,
                user_info=user_info,
                telegram_file_id=temp_file['telegram_file_id']
            )
            
            await query.edit_message_text(
                "✅ فایل شما با موفقیت برای ادمین ارسال شد!\n\n"
                "⏳ لطفاً منتظر پاسخ ادمین باشید.",
                reply_markup=self.get_user_keyboard()
            )
            
            context.user_data.clear()
            return
        
        elif data.startswith("redownload_"):
            file_code = data.replace("redownload_", "")
            
            is_blocked, remaining = self.is_temp_blocked(user.id)
            if is_blocked:
                await query.answer(f"⛔ مسدود شده‌اید. {remaining} ثانیه صبر کنید.", show_alert=True)
                return
            
            is_spam, wait_time = self.check_spam(user.id)
            if is_spam:
                await query.answer(f"⚠️ لطفاً {wait_time} ثانیه صبر کنید.", show_alert=True)
                return

            is_member, not_joined_channels = await self.check_membership(user.id)
            
            if not is_member:
                await query.answer("⚠️ هنوز در همه کانال‌ها عضو نشده‌اید!", show_alert=True)
                return
            
            if file_code not in self.files:
                await query.answer("❌ این لینک وجود ندارد.", show_alert=True)
                return
            
            await self.send_file_to_user(user.id, self.files[file_code], file_code)
            await query.answer("✅ در حال ارسال مجدد...", show_alert=False)
            return
        
        elif data == "broadcast":
            context.user_data['awaiting'] = 'broadcast_message'
            keyboard = [[InlineKeyboardButton("❌ لغو", callback_data="back_menu")]]
            await query.edit_message_text(
                "📢 لطفاً پیامی که می‌خواهید به همه کاربران ارسال شود را بنویسید:",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
            return
        
        elif data == "send_to_user":
            context.user_data['awaiting'] = 'target_user_id'
            keyboard = [[InlineKeyboardButton("❌ لغو", callback_data="back_menu")]]
            await query.edit_message_text(
                "📩 لطفاً آیدی عددی کاربر را وارد کنید:",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
            return
        
        # Admin menu options
        if data == "users":
            active_users = [u for u in self.users.values() if not u.get('is_blocked', False)]
            
            if not active_users:
                await query.edit_message_text("📋 هیچ کاربر فعالی وجود ندارد.", reply_markup=self.get_admin_keyboard())
                return
            
            message = f"👥 کاربران فعال ({len(active_users)} نفر):\n\n"
            for u in active_users[:30]:
                message += f"• {u.get('first_name', 'Unknown')} (@{u.get('username', 'none')}) - ID: {u['user_id']}\n"
            
            if len(active_users) > 30:
                message += f"\n... و {len(active_users) - 30} نفر دیگر"
            
            keyboard = [[InlineKeyboardButton("🔙 بازگشت", callback_data="back_menu")]]
            await query.edit_message_text(message, reply_markup=InlineKeyboardMarkup(keyboard))
        
        elif data == "blocked":
            blocked_users = [u for u in self.users.values() if u.get('is_blocked', False)]
            
            if not blocked_users:
                await query.edit_message_text("📋 هیچ کاربر بلاک شده‌ای وجود ندارد.", reply_markup=self.get_admin_keyboard())
                return
            
            message = f"🚫 کاربران بلاک شده ({len(blocked_users)} نفر):\n\n"
            for u in blocked_users[:30]:
                message += f"• {u.get('first_name', 'Unknown')} (@{u.get('username', 'none')}) - ID: {u['user_id']}\n"
            
            if len(blocked_users) > 30:
                message += f"\n... و {len(blocked_users) - 30} نفر دیگر"
            
            keyboard = [[InlineKeyboardButton("🔙 بازگشت", callback_data="back_menu")]]
            await query.edit_message_text(message, reply_markup=InlineKeyboardMarkup(keyboard))
        
        elif data == "channels":
            if not self.mandatory_channels:
                await query.edit_message_text("📋 هیچ کانال اجباری تنظیم نشده است.", reply_markup=self.get_admin_keyboard())
                return
            
            message = f"📢 کانال‌های عضویت اجباری ({len(self.mandatory_channels)} عدد):\n\n"
            for ch_id, ch_info in self.mandatory_channels.items():
                message += f"• {ch_info['channel_username']} (ID: {ch_id})\n"
            
            keyboard = [[InlineKeyboardButton("🔙 بازگشت", callback_data="back_menu")]]
            await query.edit_message_text(message, reply_markup=InlineKeyboardMarkup(keyboard))
        
        elif data == "add_channel":
            context.user_data['awaiting'] = 'channel_id'
            keyboard = [[InlineKeyboardButton("❌ لغو", callback_data="back_menu")]]
            await query.edit_message_text(
                "📢 لطفاً آیدی کانال یا یوزرنیم را ارسال کنید:\n\n"
                "مثال: -1001234567890\n"
                "یا: @channel_username",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        
        elif data == "remove_channel":
            context.user_data['awaiting'] = 'remove_channel_id'
            keyboard = [[InlineKeyboardButton("❌ لغو", callback_data="back_menu")]]
            await query.edit_message_text(
                "📢 لطفاً آیدی کانال برای حذف را ارسال کنید:",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        
        elif data == "add_admin":
            if user.id != MAIN_ADMIN_ID:
                await query.edit_message_text("❌ فقط ادمین اصلی می‌تواند ادمین جدید اضافه کند.", reply_markup=self.get_admin_keyboard())
                return
            
            context.user_data['awaiting'] = 'new_admin_id'
            keyboard = [[InlineKeyboardButton("❌ لغو", callback_data="back_menu")]]
            await query.edit_message_text(
                "👤 لطفاً آیدی عددی کاربر را برای افزودن به عنوان ادمین ارسال کنید:",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        
        elif data == "remove_admin":
            if user.id != MAIN_ADMIN_ID:
                await query.edit_message_text("❌ فقط ادمین اصلی می‌تواند ادمین حذف کند.", reply_markup=self.get_admin_keyboard())
                return
            
            context.user_data['awaiting'] = 'remove_admin_id'
            keyboard = [[InlineKeyboardButton("❌ لغو", callback_data="back_menu")]]
            await query.edit_message_text(
                "👤 لطفاً آیدی عددی ادمین برای حذف را ارسال کنید:",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        
        elif data == "block_user":
            context.user_data['awaiting'] = 'block_user_id'
            keyboard = [[InlineKeyboardButton("❌ لغو", callback_data="back_menu")]]
            await query.edit_message_text(
                "🔨 لطفاً آیدی عددی کاربر برای بلاک کردن را ارسال کنید:",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        
        elif data == "no_caption":
            if not self.is_admin(user.id):
                await query.edit_message_text("❌ فقط ادمین‌ها دسترسی دارند.")
                return
            
            if 'temp_file' not in context.user_data:
                await query.edit_message_text("❌ خطا: فایلی یافت نشد. لطفاً دوباره فایل را ارسال کنید.")
                context.user_data.clear()
                return
            
            temp_file = context.user_data['temp_file']
            unique_code = secrets.token_urlsafe(8)
            
            self.files[unique_code] = {
                'unique_code': unique_code,
                'file_type': temp_file['file_type'],
                'telegram_file_id': temp_file['telegram_file_id'],
                'caption': None,
                'uploaded_by': user.id,
                'created_at': datetime.now(timezone.utc).isoformat()
            }
            
            bot_username = (await self.bot.get_me()).username
            file_link = f"https://t.me/{bot_username}?start={unique_code}"
            
            keyboard = [[InlineKeyboardButton("📋 کپی لینک", url=file_link)]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await query.edit_message_text(
                f"✅ فایل بدون متن آپلود شد!\n\n"
                f"🔗 لینک:\n`{file_link}`\n\n"
                f"⚠️ توجه: با restart بات، لینک پاک می‌شود!\n"
                f"📨 پیام ارسالی به کاربر بعد از {FILE_DELETE_SECONDS} ثانیه پاک می‌شود.",
                reply_markup=reply_markup,
                parse_mode='Markdown'
            )
            
            logger.info(f"File uploaded by admin {user.id}, code: {unique_code}, no caption")
            context.user_data.clear()
        
        elif data == "back_menu":
            context.user_data.clear()
            await query.edit_message_text(
                f"👋 سلام {user.first_name}!\n\n"
                f"✨ شما ادمین هستید. برای آپلود فایل، عکس یا ویدیو را ارسال کنید.\n\n"
                f"📝 بات ابتدا فایل را دریافت می‌کند و سپس از شما متن پست را می‌خواهد.\n\n"
                f"💬 برای پاسخ به پیام کاربران، روی پیام آن‌ها Reply کنید.\n\n"
                f"⚠️ توجه: بات بدون دیتابیس است. با restart، لینک‌ها و تنظیمات پاک می‌شوند!\n\n"
                f"از دکمه‌های زیر برای مدیریت بات استفاده کنید:",
                reply_markup=self.get_admin_keyboard()
            )
        
        elif data.startswith("check_"):
            file_code = data.replace("check_", "")
            
            is_blocked, remaining = self.is_temp_blocked(user.id)
            if is_blocked:
                await query.answer(f"⛔ مسدود شده‌اید. {remaining} ثانیه صبر کنید.", show_alert=True)
                return
            
            is_spam, wait_time = self.check_spam(user.id)
            if is_spam:
                await query.answer(f"⚠️ لطفاً {wait_time} ثانیه صبر کنید.", show_alert=True)
                return
            
            is_member, not_joined_channels = await self.check_membership(user.id)
            
            if not is_member:
                await query.answer("⚠️ هنوز در همه کانال‌ها عضو نشده‌اید!", show_alert=True)
                return
            
            if file_code not in self.files:
                await query.edit_message_text("❌ این لینک وجود ندارد.")
                return
            
            await self.send_file_to_user(user.id, self.files[file_code], file_code)
            await query.edit_message_text(f"✅ فایل ارسال شد!")
            logger.info(f"File {file_code} sent to user {user.id}")
    
    async def handle_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle text messages"""
        user = update.effective_user
        text = update.message.text
        
        # Check if admin is replying
        if update.message.reply_to_message:
            is_reply_handled = await self.handle_admin_reply(update, context)
            if is_reply_handled:
                return
        
        # Handle user sending text to admin
        if context.user_data.get('awaiting') == 'user_content_to_admin':
            user_info = {
                'user_id': user.id,
                'username': user.username,
                'first_name': user.first_name
            }
            
            await self.forward_to_admins(
                message_type='text',
                content=text,
                user_info=user_info
            )
            
            await update.message.reply_text(
                "✅ پیام شما با موفقیت برای ادمین ارسال شد!\n\n"
                "⏳ لطفاً منتظر پاسخ ادمین باشید.",
                reply_markup=self.get_user_keyboard()
            )
            
            context.user_data.clear()
            return
        
        if 'awaiting' not in context.user_data:
            return
        
        awaiting = context.user_data['awaiting']
        
        if awaiting == 'broadcast_message':
            if not self.is_admin(user.id):
                return
            
            await update.message.reply_text("📤 در حال ارسال پیام به همه کاربران...")
            asyncio.create_task(self.broadcast_message(text, user.id))
            context.user_data.clear()
            return
        
        elif awaiting == 'target_user_id':
            if not self.is_admin(user.id):
                return
            
            try:
                target_user_id = int(text)
                context.user_data['target_user_id'] = target_user_id
                context.user_data['awaiting'] = 'message_to_user'
                
                await update.message.reply_text(
                    f"✅ آیدی کاربر: {target_user_id}\n\n"
                    f"📝 حالا پیام خود را بنویسید:",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ لغو", callback_data="back_menu")]])
                )
            except ValueError:
                await update.message.reply_text("❌ لطفاً یک عدد معتبر وارد کنید.", reply_markup=self.get_admin_keyboard())
                context.user_data.clear()
            return
        
        elif awaiting == 'message_to_user':
            if not self.is_admin(user.id):
                return
            
            target_user_id = context.user_data.get('target_user_id')
            if not target_user_id:
                await update.message.reply_text("❌ خطا: آیدی کاربر یافت نشد.", reply_markup=self.get_admin_keyboard())
                context.user_data.clear()
                return
            
            try:
                await self.bot.send_message(
                    chat_id=target_user_id,
                    text=f"💬 پیام از ادمین:\n\n{text}"
                )
                await update.message.reply_text(f"✅ پیام به کاربر {target_user_id} ارسال شد.", reply_markup=self.get_admin_keyboard())
            except Exception as e:
                logger.error(f"Error sending message to user {target_user_id}: {e}")
                await update.message.reply_text(f"❌ خطا در ارسال پیام به کاربر.", reply_markup=self.get_admin_keyboard())
            
            context.user_data.clear()
            return
        
        elif awaiting == 'user_caption_to_admin':
            if 'temp_user_file' not in context.user_data:
                await update.message.reply_text("❌ خطا: فایلی یافت نشد. لطفاً دوباره تلاش کنید.")
                context.user_data.clear()
                return
            
            temp_file = context.user_data['temp_user_file']
            user_info = {
                'user_id': user.id,
                'username': user.username,
                'first_name': user.first_name
            }
            
            await self.forward_to_admins(
                message_type=temp_file['file_type'],
                content=text,
                user_info=user_info,
                telegram_file_id=temp_file['telegram_file_id']
            )
            
            await update.message.reply_text(
                "✅ فایل شما با موفقیت برای ادمین ارسال شد!\n\n"
                "⏳ لطفاً منتظر پاسخ ادمین باشید.",
                reply_markup=self.get_user_keyboard()
            )
            
            context.user_data.clear()
            return
        
        elif awaiting == 'channel_id':
            if not self.is_admin(user.id):
                return
            
            if text in self.mandatory_channels:
                await update.message.reply_text("⚠️ این کانال قبلاً اضافه شده است.", reply_markup=self.get_admin_keyboard())
                context.user_data.clear()
                return
            
            self.mandatory_channels[text] = {
                'channel_id': text,
                'channel_username': text if text.startswith('@') else f"ID:{text}",
                'added_at': datetime.now(timezone.utc).isoformat()
            }
            
            await update.message.reply_text(f"✅ کانال {text} به عنوان عضویت اجباری اضافه شد.", reply_markup=self.get_admin_keyboard())
            context.user_data.clear()
        
        elif awaiting == 'remove_channel_id':
            if not self.is_admin(user.id):
                return
            
            if text in self.mandatory_channels:
                del self.mandatory_channels[text]
                await update.message.reply_text(f"✅ کانال {text} حذف شد.", reply_markup=self.get_admin_keyboard())
            else:
                await update.message.reply_text("❌ این کانال در لیست نیست.", reply_markup=self.get_admin_keyboard())
            
            context.user_data.clear()
        
        elif awaiting == 'new_admin_id':
            if user.id != MAIN_ADMIN_ID:
                return
            
            try:
                new_admin_id = int(text)
                
                if new_admin_id in self.admins:
                    await update.message.reply_text("⚠️ این کاربر قبلاً ادمین است.", reply_markup=self.get_admin_keyboard())
                    context.user_data.clear()
                    return
                
                self.admins[new_admin_id] = {
                    'user_id': new_admin_id,
                    'username': f"admin_{new_admin_id}",
                    'added_at': datetime.now(timezone.utc).isoformat()
                }
                
                await update.message.reply_text(f"✅ کاربر {new_admin_id} به عنوان ادمین اضافه شد.", reply_markup=self.get_admin_keyboard())
            except ValueError:
                await update.message.reply_text("❌ لطفاً یک عدد معتبر وارد کنید.", reply_markup=self.get_admin_keyboard())
            
            context.user_data.clear()
        
        elif awaiting == 'remove_admin_id':
            if user.id != MAIN_ADMIN_ID:
                return
            
            try:
                admin_id = int(text)
                
                if admin_id == MAIN_ADMIN_ID:
                    await update.message.reply_text("❌ نمی‌توانید ادمین اصلی را حذف کنید.", reply_markup=self.get_admin_keyboard())
                    context.user_data.clear()
                    return
                
                if admin_id in self.admins:
                    del self.admins[admin_id]
                    await update.message.reply_text(f"✅ ادمین {admin_id} حذف شد.", reply_markup=self.get_admin_keyboard())
                else:
                    await update.message.reply_text("❌ این کاربر ادمین نیست.", reply_markup=self.get_admin_keyboard())
            except ValueError:
                await update.message.reply_text("❌ لطفاً یک عدد معتبر وارد کنید.", reply_markup=self.get_admin_keyboard())
            
            context.user_data.clear()
        
        elif awaiting == 'block_user_id':
            if not self.is_admin(user.id):
                return
            
            try:
                block_user_id = int(text)
                
                if self.is_admin(block_user_id):
                    await update.message.reply_text("❌ نمی‌توانید ادمین را بلاک کنید.", reply_markup=self.get_admin_keyboard())
                    context.user_data.clear()
                    return
                
                if block_user_id in self.users:
                    self.users[block_user_id]['is_blocked'] = True
                    self.users[block_user_id]['blocked_at'] = datetime.now(timezone.utc).isoformat()
                else:
                    self.users[block_user_id] = {
                        'user_id': block_user_id,
                        'username': 'unknown',
                        'first_name': 'unknown',
                        'is_blocked': True,
                        'blocked_at': datetime.now(timezone.utc).isoformat()
                    }
                
                await update.message.reply_text(f"✅ کاربر {block_user_id} بلاک شد.", reply_markup=self.get_admin_keyboard())
            except ValueError:
                await update.message.reply_text("❌ لطفاً یک عدد معتبر وارد کنید.", reply_markup=self.get_admin_keyboard())
            
            context.user_data.clear()
        
        elif awaiting == 'caption_for_file':
            if not self.is_admin(user.id):
                return
            
            if 'temp_file' not in context.user_data:
                await update.message.reply_text("❌ خطا: فایلی یافت نشد. لطفاً دوباره فایل را ارسال کنید.")
                context.user_data.clear()
                return
            
            temp_file = context.user_data['temp_file']
            unique_code = secrets.token_urlsafe(8)
            
            self.files[unique_code] = {
                'unique_code': unique_code,
                'file_type': temp_file['file_type'],
                'telegram_file_id': temp_file['telegram_file_id'],
                'caption': text,
                'uploaded_by': user.id,
                'created_at': datetime.now(timezone.utc).isoformat()
            }
            
            bot_username = (await self.bot.get_me()).username
            file_link = f"https://t.me/{bot_username}?start={unique_code}"
            
            keyboard = [[InlineKeyboardButton("📋 کپی لینک", url=file_link)]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await update.message.reply_text(
                f"✅ فایل با موفقیت آپلود شد!\n\n"
                f"🔗 لینک:\n`{file_link}`\n\n"
                f"📝 متن پست:\n{text}\n\n"
                f"⚠️ توجه: با restart بات، لینک پاک می‌شود!\n"
                f"📨 پیام ارسالی به کاربر بعد از {FILE_DELETE_SECONDS} ثانیه پاک می‌شود.",
                reply_markup=reply_markup,
                parse_mode='Markdown'
            )
            
            logger.info(f"File uploaded by admin {user.id}, code: {unique_code}")
            context.user_data.clear()
    
    async def error_handler(self, update: object, context: ContextTypes.DEFAULT_TYPE):
        """Handle errors"""
        logger.error(f"Exception while handling an update: {context.error}")
        
        if update and hasattr(update, 'effective_user') and update.effective_user:
            if "Forbidden" in str(context.error) or "blocked" in str(context.error).lower():
                if update.effective_user.id in self.users:
                    self.users[update.effective_user.id]['is_blocked'] = True
                logger.info(f"User {update.effective_user.id} marked as blocked")
    
    def setup_handlers(self):
        """Setup all handlers"""
        self.application.add_handler(CommandHandler("start", self.start_command))
        self.application.add_handler(CallbackQueryHandler(self.button_callback))
        self.application.add_handler(MessageHandler(filters.PHOTO | filters.VIDEO, self.handle_media))
        self.application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_text))
        self.application.add_error_handler(self.error_handler)
    
    async def start(self):
        """Start the bot"""
        self.setup_handlers()
        
        await self.application.initialize()
        await self.application.start()
        await self.application.updater.start_polling()
        
        logger.info("🚀 Bot started successfully!")
        logger.info(f"📊 Main Admin ID: {MAIN_ADMIN_ID}")
        logger.info("⚠️ Running in memory mode - all data will be lost on restart!")
        
        # Keep running
        while True:
            await asyncio.sleep(1)

if __name__ == "__main__":
    bot = TelegramBot()
    asyncio.run(bot.start())
