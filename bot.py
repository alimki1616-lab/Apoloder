import logging
import asyncio
import os
from datetime import datetime, timezone, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, CallbackQueryHandler
from dotenv import load_dotenv
import secrets
import re

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
FILE_DELETE_SECONDS = 15  # Default

class TelegramBot:
    def __init__(self):
        self.application = Application.builder().token(BOT_TOKEN).build()
        self.bot = self.application.bot
        
        # In-memory storage (instead of MongoDB)
        self.users = {}  # user_id -> user_info
        self.admins = {MAIN_ADMIN_ID: {'username': 'main_admin', 'added_at': datetime.now(timezone.utc).isoformat()}}
        self.files = {}  # unique_code -> file_info (can contain multiple files)
        self.mandatory_channels = {}  # channel_identifier -> channel_info (with button_text)
        self.spam_control = {}  # user_id -> spam_info
        self.user_message_map = {}  # message_id -> user_id (for admin replies)
        self.downloads = []  # list of download records
        self.user_channel_memberships = {}  # user_id -> {channel_key: True/False}
        
    def is_admin(self, user_id: int) -> bool:
        """Check if user is admin"""
        return user_id in self.admins
    
    def extract_channel_info(self, text: str) -> dict:
        """Extract channel username or ID from link/username"""
        text = text.strip()
        
        # Check if it's a username with @ at the end (like Giftsigma@)
        if text.endswith('@'):
            text = '@' + text[:-1]
        
        # Check if it's a username (starts with @)
        if text.startswith('@'):
            return {
                'type': 'username',
                'identifier': text,
                'display': text,
                'can_auto_verify': True  # Will be determined when adding
            }
        
        # Check if it's a t.me link
        if 't.me/' in text:
            # Private link: https://t.me/+ZtfIKEcLcoM0ZThl
            if '/+' in text or 'joinchat/' in text:
                return {
                    'type': 'private_link',
                    'identifier': text,
                    'display': text,
                    'can_auto_verify': False  # Will try to verify, but may fall back to trust-based
                }
            # Public link: https://t.me/channelname
            else:
                match = re.search(r't\.me/([a-zA-Z0-9_]+)', text)
                if match:
                    username = '@' + match.group(1)
                    return {
                        'type': 'username',
                        'identifier': username,
                        'display': text,
                        'can_auto_verify': True  # Will be determined when adding
                    }
        
        # Check if it's a numeric chat_id
        if text.lstrip('-').isdigit():
            return {
                'type': 'chat_id',
                'identifier': int(text),
                'display': text,
                'can_auto_verify': True  # Will be determined when adding
            }
        
        return None
    
    async def check_if_bot_is_admin(self, channel_identifier) -> bool:
        """Check if bot is admin in the channel/group"""
        try:
            bot_info = await self.bot.get_me()
            member = await self.bot.get_chat_member(
                chat_id=channel_identifier,
                user_id=bot_info.id
            )
            return member.status in ['administrator', 'creator']
        except Exception as e:
            logger.warning(f"Cannot check if bot is admin in {channel_identifier}: {e}")
            return False
    
    async def check_membership(self, user_id: int) -> tuple[bool, list]:
        """Check if user is member of all mandatory channels"""
        if not self.mandatory_channels:
            return True, []
        
        # Initialize user membership tracking if not exists
        if user_id not in self.user_channel_memberships:
            self.user_channel_memberships[user_id] = {}
        
        not_joined = []
        for channel_key, channel_info in self.mandatory_channels.items():
            try:
                # Check if we already verified this user for this channel
                if self.user_channel_memberships[user_id].get(channel_key):
                    # Already verified, skip (unless we need to recheck for left)
                    # If bot is admin, we can recheck
                    if channel_info.get('can_auto_verify'):
                        # Recheck to see if user left
                        identifier = channel_info.get('identifier')
                        try:
                            member = await self.bot.get_chat_member(
                                chat_id=identifier,
                                user_id=user_id
                            )
                            if member.status not in ['member', 'administrator', 'creator']:
                                # User left, mark as not joined
                                self.user_channel_memberships[user_id][channel_key] = False
                                not_joined.append(channel_info)
                            # else: still member, keep verified status
                        except Exception as e:
                            logger.warning(f"Cannot recheck membership for {identifier}: {e}")
                            # Keep existing verified status
                    # else: trust-based, keep verified
                    continue
                
                identifier = channel_info.get('identifier')
                channel_type = channel_info.get('type')
                can_auto_verify = channel_info.get('can_auto_verify', False)
                
                # If bot is admin in channel, do automatic verification
                if can_auto_verify:
                    try:
                        member = await self.bot.get_chat_member(
                            chat_id=identifier,
                            user_id=user_id
                        )
                        if member.status in ['member', 'administrator', 'creator']:
                            # Mark as verified
                            self.user_channel_memberships[user_id][channel_key] = True
                            logger.info(f"User {user_id} verified automatically in {identifier}")
                        else:
                            # Not joined or kicked
                            self.user_channel_memberships[user_id][channel_key] = False
                            not_joined.append(channel_info)
                    except Exception as e:
                        logger.warning(f"Cannot auto-check membership for {identifier}: {e}")
                        # Cannot verify, ask user to click
                        if not self.user_channel_memberships[user_id].get(channel_key):
                            not_joined.append(channel_info)
                else:
                    # Bot is not admin - require manual confirmation (trust-based after click)
                    if not self.user_channel_memberships[user_id].get(channel_key):
                        not_joined.append(channel_info)
                    
            except Exception as e:
                logger.error(f"Error checking membership for channel {channel_key}: {e}")
                if not self.user_channel_memberships[user_id].get(channel_key):
                    not_joined.append(channel_info)
        
        return len(not_joined) == 0, not_joined
    
    def mark_user_joined_channel(self, user_id: int, channel_key: str):
        """Mark that user has joined a channel"""
        if user_id not in self.user_channel_memberships:
            self.user_channel_memberships[user_id] = {}
        self.user_channel_memberships[user_id][channel_key] = True
    
    def get_channel_url(self, channel_info: dict) -> str:
        """Convert channel info to a valid URL"""
        display = channel_info.get('display', '')
        
        # If it's already a URL, return it
        if display.startswith('http'):
            return display
        
        # If it's a username starting with @, convert to URL
        if display.startswith('@'):
            username = display[1:]  # Remove @
            return f"https://t.me/{username}"
        
        # Default: return as is (might be a numeric ID, but we'll handle that)
        return display
    
    async def schedule_message_deletion_and_send_buttons(self, chat_id: int, message_ids: list, delay_seconds: int, file_code: str = None):
        """Delete messages after specified seconds and send buttons"""
        await asyncio.sleep(delay_seconds)
        
        try:
            # Delete all messages
            for message_id in message_ids:
                try:
                    await self.bot.delete_message(chat_id=chat_id, message_id=message_id)
                    logger.info(f"Message {message_id} deleted from chat {chat_id} after {delay_seconds} seconds")
                except Exception as e:
                    logger.error(f"Error deleting message {message_id}: {e}")
            
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
            logger.error(f"Error in deletion process: {e}")

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
            [InlineKeyboardButton("📋 لیست لینک‌های فایل", callback_data="list_files"),
             InlineKeyboardButton("🗑 حذف لینک فایل", callback_data="expire_file")],
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
        """Check if user is spamming - improved version"""
        now = datetime.now(timezone.utc)
        
        if user_id in self.spam_control:
            spam_info = self.spam_control[user_id]
            last_request = datetime.fromisoformat(spam_info['last_request'])
            time_diff = (now - last_request).total_seconds()
            
            # If less than 2 seconds between requests, count as spam
            if time_diff < 2:
                request_count = spam_info.get('request_count', 0) + 1
                
                self.spam_control[user_id] = {
                    'request_count': request_count,
                    'last_request': now.isoformat(),
                    'blocked_until': (now + timedelta(seconds=10)).isoformat() if request_count >= 5 else None
                }
                
                # Block for 10 seconds if 5 rapid requests
                if request_count >= 5:
                    return True, 10
                
                return True, int(2 - time_diff)
            else:
                # Reset counter if more than 2 seconds passed
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
                self.spam_control[user_id]['request_count'] = 0
        
        return False, 0
    
    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /start command"""
        user = update.effective_user

        # Check if user is blocked
        if user.id in self.users and self.users[user.id].get('is_blocked', False):
            keyboard = [[InlineKeyboardButton("📞 ارتباط با مدیر", callback_data="contact_admin")]]
            await update.message.reply_text(
                "⛔ شما توسط ادمین بلاک شده‌اید.\n\n"
                "برای رفع مسدودیت با ادمین تماس بگیرید.\n\n"
                "می‌توانید از دکمه زیر استفاده کنید:",
                reply_markup=InlineKeyboardMarkup(keyboard)
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
                f"📝 می‌توانید چند فایل پشت سر هم ارسال کنید و یک لینک واحد دریافت کنید.\n\n"
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
        
        # Skip spam check for admins
        if not self.is_admin(user.id):
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
                if wait_time >= 10:
                    await update.message.reply_text(
                        f"⛔ شما به دلیل اسپم برای 10 ثانیه مسدود شدید!\n\n"
                        "لطفاً صبر کنید."
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
                # Show custom button text with channel link (convert to URL)
                channel_url = self.get_channel_url(channel)
                keyboard.append([InlineKeyboardButton(
                    channel['button_text'],
                    url=channel_url
                )])
            keyboard.append([InlineKeyboardButton(
                "عضو شدم ✅",
                callback_data=f"check_{file_code}"
            )])
            
            reply_markup = InlineKeyboardMarkup(keyboard)
            await update.message.reply_text(
                "⚠️ برای دریافت فایل، ابتدا باید در کانال‌ها/گروه‌های زیر عضو شوید:\n\n"
                "👇 روی دکمه‌های زیر کلیک کنید و عضو شوید، سپس «عضو شدم ✅» را بزنید:",
                reply_markup=reply_markup
            )
            return
        
        # Send files
        await self.send_files_to_user(user.id, self.files[file_code], file_code)
    
    async def send_files_to_user(self, user_id: int, file_group: dict, file_code: str):
        """Send multiple files to user"""
        try:
            files_list = file_group['files']  # List of files
            caption_text = file_group.get('caption', '')
            delete_seconds = file_group.get('delete_seconds', FILE_DELETE_SECONDS)
            
            sent_message_ids = []
            
            for idx, file_doc in enumerate(files_list):
                # Add caption only to first file
                if idx == 0 and caption_text:
                    full_caption = f"{caption_text}\n\n⏱️ این محتوا بعد از {delete_seconds} ثانیه پاک می‌شود!"
                else:
                    full_caption = f"⏱️ این محتوا بعد از {delete_seconds} ثانیه پاک می‌شود!"
                
                sent_message = None
                
                if file_doc['file_type'] == 'photo':
                    sent_message = await self.bot.send_photo(
                        chat_id=user_id,
                        photo=file_doc['telegram_file_id'],
                        caption=full_caption if idx == 0 or not caption_text else None
                    )
                elif file_doc['file_type'] == 'video':
                    sent_message = await self.bot.send_video(
                        chat_id=user_id,
                        video=file_doc['telegram_file_id'],
                        caption=full_caption if idx == 0 or not caption_text else None
                    )
                
                if sent_message:
                    sent_message_ids.append(sent_message.message_id)
            
            # Schedule deletion for all messages
            if sent_message_ids:
                asyncio.create_task(
                    self.schedule_message_deletion_and_send_buttons(
                        chat_id=user_id,
                        message_ids=sent_message_ids,
                        delay_seconds=delete_seconds,
                        file_code=file_code
                    )
                )
            
            # Track download
            self.downloads.append({
                'file_code': file_code,
                'user_id': user_id,
                'downloaded_at': datetime.now(timezone.utc).isoformat()
            })
            
            logger.info(f"Files {file_code} sent to user {user_id}")
        except Exception as e:
            logger.error(f"Error sending files: {e}")
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
        
        # Initialize temp_files list if not exists
        if 'temp_files' not in context.user_data:
            context.user_data['temp_files'] = []
        
        # Add file to list
        context.user_data['temp_files'].append({
            'file_type': file_type,
            'telegram_file_id': telegram_file_id
        })
        
        file_count = len(context.user_data['temp_files'])
        
        # Ask if user wants to add more files
        keyboard = [
            [InlineKeyboardButton("✅ بله، فایل دیگری هم دارم", callback_data="add_more_files")],
            [InlineKeyboardButton("❌ نه، تمام شد", callback_data="finish_files")],
            [InlineKeyboardButton("🗑 لغو و پاک کردن همه", callback_data="cancel_upload")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await update.message.reply_text(
            f"✅ فایل {file_count} دریافت شد!\n\n"
            f"📦 تعداد فایل‌های دریافت شده: {file_count}\n\n"
            "فایل دیگری هم دارید؟",
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
        
        # Check admin permission for admin-only actions
        admin_actions = ['users', 'blocked', 'channels', 'add_channel', 'remove_channel', 
                        'add_admin', 'remove_admin', 'block_user', 'broadcast', 
                        'send_to_user', 'list_files', 'expire_file']
        
        if data in admin_actions:
            if not self.is_admin(user.id):
                await query.edit_message_text("❌ فقط ادمین‌ها دسترسی دارند.")
                return
        
        # Handle file upload flow
        if data == "add_more_files":
            await query.edit_message_text(
                f"📤 در انتظار فایل بعدی...\n\n"
                f"📦 تعداد فایل‌های دریافت شده: {len(context.user_data.get('temp_files', []))}\n\n"
                "لطفاً فایل بعدی را ارسال کنید."
            )
            return
        
        elif data == "finish_files":
            if 'temp_files' not in context.user_data or not context.user_data['temp_files']:
                await query.edit_message_text("❌ خطا: فایلی یافت نشد.")
                context.user_data.clear()
                return
            
            context.user_data['awaiting'] = 'caption_for_files'
            keyboard = [[InlineKeyboardButton("🚫 بدون متن", callback_data="no_caption_files")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await query.edit_message_text(
                f"✅ {len(context.user_data['temp_files'])} فایل دریافت شد!\n\n"
                "📝 لطفاً یک متن واحد برای همه فایل‌ها ارسال کنید:\n\n"
                "یا روی دکمه «بدون متن» کلیک کنید.",
                reply_markup=reply_markup
            )
            return
        
        elif data == "cancel_upload":
            context.user_data.clear()
            await query.edit_message_text(
                "🗑 آپلود لغو شد و همه فایل‌ها پاک شدند.",
                reply_markup=self.get_admin_keyboard()
            )
            return
        
        elif data == "no_caption_files":
            if 'temp_files' not in context.user_data or not context.user_data['temp_files']:
                await query.edit_message_text("❌ خطا: فایلی یافت نشد.")
                context.user_data.clear()
                return
            
            context.user_data['caption'] = None
            context.user_data['awaiting'] = 'delete_time'
            
            await query.edit_message_text(
                "⏱️ چه مدت بعد محتوا پاک شود؟\n\n"
                "لطفاً یک عدد بین 5 تا 30 (ثانیه) وارد کنید:\n\n"
                "مثال: 10"
            )
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
                "برای دریافت فایل‌ها، لینک را از ادمین دریافت کنید.\n\n"
                "یا می‌توانید از دکمه زیر برای ارتباط با مدیر استفاده کنید:",
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
            
            # Skip spam check for admins
            if not self.is_admin(user.id):
                is_blocked, remaining = self.is_temp_blocked(user.id)
                if is_blocked:
                    await query.answer(f"⛔ مسدود شده‌اید. {remaining} ثانیه صبر کنید.", show_alert=True)
                    return
                
                is_spam, wait_time = self.check_spam(user.id)
                if is_spam:
                    await query.answer(f"⚠️ لطفاً {wait_time} ثانیه صبر کنید.", show_alert=True)
                    return

            # Check membership again
            is_member, not_joined_channels = await self.check_membership(user.id)
            
            if not is_member:
                await query.answer("⚠️ هنوز در همه کانال‌ها عضو نشده‌اید!", show_alert=True)
                
                # Show join buttons again
                keyboard = []
                for channel in not_joined_channels:
                    channel_url = self.get_channel_url(channel)
                    keyboard.append([InlineKeyboardButton(
                        channel['button_text'],
                        url=channel_url
                    )])
                keyboard.append([InlineKeyboardButton(
                    "عضو شدم ✅",
                    callback_data=f"check_{file_code}"
                )])
                
                await query.edit_message_text(
                    "⚠️ برای دریافت فایل، ابتدا باید در کانال‌ها/گروه‌های زیر عضو شوید:\n\n"
                    "👇 روی دکمه‌های زیر کلیک کنید و عضو شوید، سپس «عضو شدم ✅» را بزنید:",
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )
                return
            
            if file_code not in self.files:
                await query.answer("❌ این لینک وجود ندارد.", show_alert=True)
                return
            
            await self.send_files_to_user(user.id, self.files[file_code], file_code)
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
        elif data == "users":
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
                keyboard = [[InlineKeyboardButton("🔙 بازگشت", callback_data="back_menu")]]
                await query.edit_message_text(
                    "📋 هیچ کانال اجباری تنظیم نشده است.", 
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )
                return
            
            message = f"📢 کانال‌های عضویت اجباری ({len(self.mandatory_channels)} عدد):\n\n"
            for idx, (ch_key, ch_info) in enumerate(self.mandatory_channels.items(), 1):
                verify_mode = "✅ چک خودکار" if ch_info.get('can_auto_verify') else "👆 تایید دستی"
                message += f"{idx}. {ch_info['button_text']}\n"
                message += f"   🔗 {ch_info['display']}\n"
                message += f"   🔍 {verify_mode}\n\n"
            
            keyboard = [[InlineKeyboardButton("🔙 بازگشت", callback_data="back_menu")]]
            await query.edit_message_text(message, reply_markup=InlineKeyboardMarkup(keyboard))
        
        elif data == "add_channel":
            context.user_data['awaiting'] = 'channel_link'
            keyboard = [[InlineKeyboardButton("❌ لغو", callback_data="back_menu")]]
            await query.edit_message_text(
                "📢 لینک یا یوزرنیم کانال را ارسال کنید\n\n"
                "✅ فرمت‌های قابل قبول:\n"
                "• @channelname\n"
                "• https://t.me/channelname\n"
                "• https://t.me/+ZtfIKEcLcoM0ZThl (لینک خصوصی)\n\n"
                "💡 نکته: بات خودکار تشخیص می‌دهد که ادمین است یا نه.",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        
        elif data == "remove_channel":
            if not self.mandatory_channels:
                keyboard = [[InlineKeyboardButton("🔙 بازگشت", callback_data="back_menu")]]
                await query.edit_message_text(
                    "📋 هیچ کانال اجباری وجود ندارد.", 
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )
                return
            
            context.user_data['awaiting'] = 'remove_channel_key'
            
            message = "📢 لیست کانال‌ها:\n\n"
            for idx, (ch_key, ch_info) in enumerate(self.mandatory_channels.items(), 1):
                message += f"{idx}. {ch_info['button_text']}\n"
                message += f"   🔗 {ch_info['display']}\n\n"
            
            message += "لطفاً شماره کانال را برای حذف ارسال کنید:"
            
            keyboard = [[InlineKeyboardButton("❌ لغو", callback_data="back_menu")]]
            await query.edit_message_text(message, reply_markup=InlineKeyboardMarkup(keyboard))
        
        elif data == "list_files":
            if not self.files:
                keyboard = [[InlineKeyboardButton("🔙 بازگشت", callback_data="back_menu")]]
                await query.edit_message_text(
                    "📋 هیچ لینک فایلی وجود ندارد.", 
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )
                return
            
            try:
                bot_username = (await self.bot.get_me()).username
                message_parts = []
                current_message = f"📋 لیست لینک‌های فایل ({len(self.files)} عدد):\n\n"
                
                for idx, (code, file_info) in enumerate(self.files.items(), 1):
                    file_count = len(file_info.get('files', []))
                    caption = file_info.get('caption', 'بدون متن')
                    if len(caption) > 30:
                        caption = caption[:30] + "..."
                    delete_time = file_info.get('delete_seconds', 15)
                    
                    file_entry = (
                        f"{idx}. کد: {code}\n"
                        f"   📦 تعداد فایل: {file_count}\n"
                        f"   📝 متن: {caption}\n"
                        f"   ⏱️ زمان حذف: {delete_time}s\n"
                        f"   🔗 https://t.me/{bot_username}?start={code}\n\n"
                    )
                    
                    # Check if adding this entry would exceed message limit
                    if len(current_message + file_entry) > 3500:
                        message_parts.append(current_message)
                        current_message = file_entry
                    else:
                        current_message += file_entry
                    
                    if idx >= 20:  # Limit to 20 files
                        current_message += f"... و {len(self.files) - 20} لینک دیگر"
                        break
                
                message_parts.append(current_message)
                
                # Send first part with back button
                keyboard = [[InlineKeyboardButton("🔙 بازگشت", callback_data="back_menu")]]
                await query.edit_message_text(
                    message_parts[0], 
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )
                
                # Send additional parts if needed
                for part in message_parts[1:]:
                    await self.bot.send_message(
                        chat_id=user.id,
                        text=part
                    )
                    
            except Exception as e:
                logger.error(f"Error in list_files: {e}")
                keyboard = [[InlineKeyboardButton("🔙 بازگشت", callback_data="back_menu")]]
                await query.edit_message_text(
                    "❌ خطا در نمایش لیست فایل‌ها.",
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )
        
        elif data == "expire_file":
            if not self.files:
                keyboard = [[InlineKeyboardButton("🔙 بازگشت", callback_data="back_menu")]]
                await query.edit_message_text(
                    "📋 هیچ لینک فایلی برای حذف وجود ندارد.", 
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )
                return
            
            context.user_data['awaiting'] = 'expire_file_code'
            
            message = "🗑 لیست لینک‌های فایل:\n\n"
            
            for idx, (code, file_info) in enumerate(self.files.items(), 1):
                file_count = len(file_info.get('files', []))
                caption = file_info.get('caption', 'بدون متن')
                if len(caption) > 30:
                    caption = caption[:30] + "..."
                
                message += f"{idx}. کد: {code}\n"
                message += f"   📦 {file_count} فایل - {caption}\n\n"
                
                if idx >= 10:
                    message += f"... و {len(self.files) - 10} لینک دیگر\n\n"
                    break
            
            message += "لطفاً کد فایل را برای منقضی کردن ارسال کنید:"
            
            keyboard = [[InlineKeyboardButton("❌ لغو", callback_data="back_menu")]]
            await query.edit_message_text(message, reply_markup=InlineKeyboardMarkup(keyboard))
        
        elif data == "add_admin":
            if user.id != MAIN_ADMIN_ID:
                await query.edit_message_text(
                    "❌ فقط ادمین اصلی می‌تواند ادمین جدید اضافه کند.", 
                    reply_markup=self.get_admin_keyboard()
                )
                return
            
            context.user_data['awaiting'] = 'new_admin_id'
            keyboard = [[InlineKeyboardButton("❌ لغو", callback_data="back_menu")]]
            await query.edit_message_text(
                "👤 لطفاً آیدی عددی کاربر را برای افزودن به عنوان ادمین ارسال کنید:",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        
        elif data == "remove_admin":
            if user.id != MAIN_ADMIN_ID:
                await query.edit_message_text(
                    "❌ فقط ادمین اصلی می‌تواند ادمین حذف کند.", 
                    reply_markup=self.get_admin_keyboard()
                )
                return
            
            # Show list of current admins
            admin_list = "👥 لیست ادمین‌های فعلی:\n\n"
            for admin_id in self.admins.keys():
                if admin_id == MAIN_ADMIN_ID:
                    admin_list += f"• {admin_id} (ادمین اصلی) ⭐\n"
                else:
                    admin_list += f"• {admin_id}\n"
            
            admin_list += "\n💡 لطفاً آیدی عددی ادمین برای حذف را ارسال کنید:"
            
            context.user_data['awaiting'] = 'remove_admin_id'
            keyboard = [[InlineKeyboardButton("❌ لغو", callback_data="back_menu")]]
            await query.edit_message_text(
                admin_list,
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        
        elif data == "block_user":
            context.user_data['awaiting'] = 'block_user_id'
            keyboard = [[InlineKeyboardButton("❌ لغو", callback_data="back_menu")]]
            await query.edit_message_text(
                "🔨 لطفاً آیدی عددی کاربر برای بلاک کردن را ارسال کنید:",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        
        elif data == "back_menu":
            context.user_data.clear()
            await query.edit_message_text(
                f"👋 سلام {user.first_name}!\n\n"
                "✨ شما ادمین هستید. برای آپلود فایل، عکس یا ویدیو را ارسال کنید.\n\n"
                "📝 می‌توانید چند فایل پشت سر هم ارسال کنید و یک لینک واحد دریافت کنید.\n\n"
                "💬 برای پاسخ به پیام کاربران، روی پیام آن‌ها Reply کنید.\n\n"
                "⚠️ توجه: بات بدون دیتابیس است. با restart، لینک‌ها و تنظیمات پاک می‌شوند!\n\n"
                "از دکمه‌های زیر برای مدیریت بات استفاده کنید:",
                reply_markup=self.get_admin_keyboard()
            )
        
        elif data.startswith("check_"):
            file_code = data.replace("check_", "")
            
            # Skip spam check for admins
            if not self.is_admin(user.id):
                is_blocked, remaining = self.is_temp_blocked(user.id)
                if is_blocked:
                    await query.answer(f"⛔ مسدود شده‌اید. {remaining} ثانیه صبر کنید.", show_alert=True)
                    return
                
                is_spam, wait_time = self.check_spam(user.id)
                if is_spam:
                    await query.answer(f"⚠️ لطفاً {wait_time} ثانیه صبر کنید.", show_alert=True)
                    return
            
            # Check membership again - with improved tracking
            is_member, not_joined_channels = await self.check_membership(user.id)
            
            # If user clicked "I joined", process accordingly
            if not_joined_channels:
                # Separate verifiable and trust-based channels
                still_not_joined = []
                trust_channels = []
                
                for channel in not_joined_channels:
                    channel_key = str(channel.get('identifier'))
                    # If bot is admin (can auto-verify) and still fails, keep in list
                    if channel.get('can_auto_verify'):
                        still_not_joined.append(channel)
                    else:
                        # Bot is NOT admin - trust the user after they click the links
                        trust_channels.append(channel)
                        self.mark_user_joined_channel(user.id, channel_key)
                
                if still_not_joined:
                    # Some channels where bot IS admin but user still not joined
                    await query.answer("⚠️ هنوز در برخی کانال‌ها عضو نشده‌اید! (بات می‌تواند چک کند)", show_alert=True)
                    return
                elif trust_channels:
                    # All remaining channels are trust-based (bot not admin)
                    # Mark them as joined after user clicked
                    is_member = True
                    logger.info(f"User {user.id} verified via trust-based method for {len(trust_channels)} channels")
            
            if file_code not in self.files:
                await query.edit_message_text("❌ این لینک وجود ندارد.")
                return
            
            await self.send_files_to_user(user.id, self.files[file_code], file_code)
            await query.edit_message_text("✅ فایل ارسال شد!")
            logger.info(f"Files {file_code} sent to user {user.id}")
    
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
                    "📝 حالا پیام خود را بنویسید:",
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
                await update.message.reply_text("❌ خطا در ارسال پیام به کاربر.", reply_markup=self.get_admin_keyboard())
            
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
        
        elif awaiting == 'channel_link':
            if not self.is_admin(user.id):
                return
            
            channel_info = self.extract_channel_info(text)
            
            if not channel_info:
                await update.message.reply_text(
                    "❌ فرمت نامعتبر!\n\n"
                    "✅ فرمت‌های قابل قبول:\n"
                    "• @channelname\n"
                    "• Giftsigma@ (یوزرنیم با @ در آخر)\n"
                    "• https://t.me/channelname\n"
                    "• https://t.me/+ZtfIKEcLcoM0ZThl\n\n"
                    "لطفاً دوباره ارسال کنید یا /start را بزنید.",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ لغو", callback_data="back_menu")]])
                )
                return
            
            # Check if bot is admin in this channel
            is_bot_admin = await self.check_if_bot_is_admin(channel_info['identifier'])
            channel_info['can_auto_verify'] = is_bot_admin
            
            # Store temporarily
            context.user_data['temp_channel_info'] = channel_info
            context.user_data['awaiting'] = 'channel_button_text'
            
            verification_mode = "✅ چک خودکار (بات ادمین است)" if is_bot_admin else "👆 تایید دستی (بات ادمین نیست)"
            
            await update.message.reply_text(
                f"✅ کانال دریافت شد!\n\n"
                f"🔗 {channel_info['display']}\n"
                f"🔍 نوع تایید: {verification_mode}\n\n"
                "📢 حالا متن دکمه را بنویسید:\n\n"
                "مثال: «عضویت در کانال» یا «جوین شو 👇»"
            )
            return
        
        elif awaiting == 'channel_button_text':
            if not self.is_admin(user.id):
                return
            
            channel_info = context.user_data.get('temp_channel_info')
            
            if not channel_info:
                await update.message.reply_text("❌ خطا: اطلاعات کانال یافت نشد.", reply_markup=self.get_admin_keyboard())
                context.user_data.clear()
                return
            
            button_text = text
            
            try:
                # Use identifier as key
                key = str(channel_info['identifier'])
                
                # Store channel info - this should persist!
                self.mandatory_channels[key] = {
                    'type': channel_info['type'],
                    'identifier': channel_info['identifier'],
                    'display': channel_info['display'],
                    'button_text': button_text,
                    'can_auto_verify': channel_info.get('can_auto_verify', False),
                    'added_at': datetime.now(timezone.utc).isoformat()
                }
                
                verification_mode = "✅ چک خودکار" if channel_info.get('can_auto_verify') else "👆 تایید دستی توسط کاربر"
                
                await update.message.reply_text(
                    f"✅ کانال با موفقیت اضافه شد!\n\n"
                    f"🔗 {channel_info['display']}\n"
                    f"📝 متن دکمه: {button_text}\n"
                    f"🔍 نوع تایید: {verification_mode}\n\n"
                    f"📊 تعداد کانال‌های فعال: {len(self.mandatory_channels)}",
                    reply_markup=self.get_admin_keyboard()
                )
                
                logger.info(f"Channel added: {channel_info['display']} with button text: {button_text}, auto_verify: {channel_info.get('can_auto_verify')}, total channels: {len(self.mandatory_channels)}")
            except Exception as e:
                logger.error(f"Error adding channel: {e}")
                await update.message.reply_text(
                    "❌ خطا در افزودن کانال.",
                    reply_markup=self.get_admin_keyboard()
                )
            
            context.user_data.clear()
            return
        
        elif awaiting == 'remove_channel_key':
            if not self.is_admin(user.id):
                return
            
            # Check if it's a number (index)
            if text.isdigit():
                index = int(text) - 1
                if 0 <= index < len(self.mandatory_channels):
                    key_to_remove = list(self.mandatory_channels.keys())[index]
                    removed_channel = self.mandatory_channels[key_to_remove]
                    del self.mandatory_channels[key_to_remove]
                    
                    await update.message.reply_text(
                        f"✅ کانال حذف شد!\n\n"
                        f"🔗 {removed_channel.get('display', 'Unknown')}\n"
                        f"📊 کانال‌های باقی‌مانده: {len(self.mandatory_channels)}",
                        reply_markup=self.get_admin_keyboard()
                    )
                    logger.info(f"Channel removed: {removed_channel.get('display')}, remaining: {len(self.mandatory_channels)}")
                else:
                    await update.message.reply_text("❌ شماره نامعتبر است.", reply_markup=self.get_admin_keyboard())
            else:
                await update.message.reply_text("❌ لطفاً شماره کانال را وارد کنید.", reply_markup=self.get_admin_keyboard())
            
            context.user_data.clear()
            return
        
        elif awaiting == 'expire_file_code':
            if not self.is_admin(user.id):
                return
            
            file_code = text.strip()
            
            if file_code in self.files:
                del self.files[file_code]
                await update.message.reply_text(
                    f"✅ لینک فایل {file_code} با موفقیت منقضی شد!\n\n"
                    "🔗 این لینک دیگر قابل استفاده نیست.",
                    reply_markup=self.get_admin_keyboard()
                )
                logger.info(f"File link {file_code} expired by admin {user.id}")
            else:
                await update.message.reply_text(
                    "❌ کد فایل پیدا نشد.\n\n"
                    "لطفاً از لیست لینک‌ها کد صحیح را کپی کنید.",
                    reply_markup=self.get_admin_keyboard()
                )
            
            context.user_data.clear()
            return
        
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
                logger.info(f"New admin added: {new_admin_id}")
            except ValueError:
                await update.message.reply_text("❌ لطفاً یک عدد معتبر وارد کنید.", reply_markup=self.get_admin_keyboard())
            
            context.user_data.clear()
            return
        
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
                    logger.info(f"Admin removed: {admin_id}")
                else:
                    await update.message.reply_text("❌ این کاربر ادمین نیست.", reply_markup=self.get_admin_keyboard())
            except ValueError:
                await update.message.reply_text("❌ لطفاً یک عدد معتبر وارد کنید.", reply_markup=self.get_admin_keyboard())
            
            context.user_data.clear()
            return
        
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
                logger.info(f"User blocked: {block_user_id}")
            except ValueError:
                await update.message.reply_text("❌ لطفاً یک عدد معتبر وارد کنید.", reply_markup=self.get_admin_keyboard())
            
            context.user_data.clear()
            return
        
        elif awaiting == 'caption_for_files':
            if not self.is_admin(user.id):
                return
            
            if 'temp_files' not in context.user_data or not context.user_data['temp_files']:
                await update.message.reply_text("❌ خطا: فایلی یافت نشد.")
                context.user_data.clear()
                return
            
            context.user_data['caption'] = text
            context.user_data['awaiting'] = 'delete_time'
            
            await update.message.reply_text(
                "⏱️ چه مدت بعد محتوا پاک شود؟\n\n"
                "لطفاً یک عدد بین 5 تا 30 (ثانیه) وارد کنید:\n\n"
                "مثال: 10"
            )
            return
        
        elif awaiting == 'delete_time':
            if not self.is_admin(user.id):
                return
            
            try:
                delete_seconds = int(text)
                
                if delete_seconds < 5 or delete_seconds > 30:
                    await update.message.reply_text(
                        "❌ عدد باید بین 5 تا 30 باشد.\n\n"
                        "لطفاً دوباره امتحان کنید:"
                    )
                    return
                
                # Create file group with all files
                unique_code = secrets.token_urlsafe(8)
                
                self.files[unique_code] = {
                    'unique_code': unique_code,
                    'files': context.user_data['temp_files'],
                    'caption': context.user_data.get('caption'),
                    'delete_seconds': delete_seconds,
                    'uploaded_by': user.id,
                    'created_at': datetime.now(timezone.utc).isoformat()
                }
                
                bot_username = (await self.bot.get_me()).username
                file_link = f"https://t.me/{bot_username}?start={unique_code}"
                
                keyboard = [[InlineKeyboardButton("📋 کپی لینک", url=file_link)]]
                reply_markup = InlineKeyboardMarkup(keyboard)
                
                caption_preview = context.user_data.get('caption', 'بدون متن')
                
                await update.message.reply_text(
                    f"✅ {len(context.user_data['temp_files'])} فایل با موفقیت آپلود شد!\n\n"
                    f"🔗 لینک:\n{file_link}\n\n"
                    f"📝 متن پست: {caption_preview}\n\n"
                    f"⏱️ زمان حذف: {delete_seconds} ثانیه\n\n"
                    "⚠️ توجه: با restart بات، لینک پاک می‌شود!",
                    reply_markup=reply_markup
                )
                
                logger.info(f"Files uploaded by admin {user.id}, code: {unique_code}, count: {len(context.user_data['temp_files'])}, delete_time: {delete_seconds}s")
                context.user_data.clear()
                
            except ValueError:
                await update.message.reply_text(
                    "❌ لطفاً یک عدد معتبر وارد کنید (5-30).\n\n"
                    "مثال: 15"
                )
            return
    
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
