import logging
import asyncio
import os
from datetime import datetime, timezone, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton, ChatMemberUpdated
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, CallbackQueryHandler, ChatMemberHandler
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
        self.detected_channels = {}  # chat_id -> channel_info (auto-detected when bot becomes admin)
        
    def is_admin(self, user_id: int) -> bool:
        """Check if user is admin"""
        return user_id in self.admins
    
    def get_user_keyboard(self):
        """Create user reply keyboard"""
        keyboard = [
            [KeyboardButton("📞 ارتباط با مدیر")]
        ]
        return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    
    def get_admin_keyboard(self):
        """Create admin reply keyboard"""
        keyboard = [
            [KeyboardButton("👥 کاربران"), KeyboardButton("📁 فایل‌ها")],
            [KeyboardButton("📨 ارسال PM"), KeyboardButton("🔒 جوین اجباری")],
        ]
        return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    
    async def get_chat_id_from_link(self, link: str):
        """Try to get actual chat_id from a link by calling getChat"""
        try:
            # For private links like t.me/+abc123, we can't get chat_id without being member
            # But if bot is already admin, we can try to use the link directly
            
            # Extract invite hash from private link
            if '/+' in link:
                # This is a private invite link - we need to be member first
                return None
            
            # For public channels, extract username
            if 't.me/' in link:
                match = re.search(r't\.me/([a-zA-Z0-9_]+)', link)
                if match:
                    username = '@' + match.group(1)
                    chat = await self.bot.get_chat(chat_id=username)
                    return chat.id
            
            return None
        except Exception as e:
            logger.warning(f"Cannot get chat_id from link {link}: {e}")
            return None
    
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
    
    async def check_if_bot_is_admin(self, channel_identifier) -> tuple[bool, int, str]:
        """
        Check if bot is admin in the channel/group
        Returns: (is_admin, chat_id or None, invite_link or None)
        """
        try:
            bot_info = await self.bot.get_me()
            
            # Try to get chat info first
            chat = None
            chat_id = None
            invite_link = None
            
            if isinstance(channel_identifier, int):
                chat_id = channel_identifier
            elif isinstance(channel_identifier, str):
                if channel_identifier.startswith('@'):
                    try:
                        chat = await self.bot.get_chat(chat_id=channel_identifier)
                        chat_id = chat.id
                    except Exception as e:
                        logger.warning(f"Cannot get chat for {channel_identifier}: {e}")
                        return False, None, None
                else:
                    # It's a link - try to extract username or use detected channels
                    for detected_chat_id, detected_info in self.detected_channels.items():
                        if detected_info.get('invite_link') == channel_identifier or detected_info.get('display') == channel_identifier:
                            chat_id = detected_chat_id
                            invite_link = detected_info.get('invite_link')
                            break
                    
                    if not chat_id:
                        # Try to get from link
                        chat_id = await self.get_chat_id_from_link(channel_identifier)
            
            if not chat_id:
                return False, None, None
            
            # Check if bot is admin
            member = await self.bot.get_chat_member(
                chat_id=chat_id,
                user_id=bot_info.id
            )
            is_admin = member.status in ['administrator', 'creator']
            
            # If bot is admin, try to get invite link
            if is_admin and not invite_link:
                try:
                    invite_link = await self.bot.export_chat_invite_link(chat_id=chat_id)
                    logger.info(f"Got invite link for chat {chat_id}: {invite_link}")
                except Exception as e:
                    logger.warning(f"Cannot export invite link for {chat_id}: {e}")
            
            return is_admin, chat_id, invite_link
            
        except Exception as e:
            logger.warning(f"Cannot check if bot is admin in {channel_identifier}: {e}")
            return False, None, None
    
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
                # Get the actual chat_id to check
                chat_id = channel_info.get('chat_id') or channel_info.get('identifier')
                can_auto_verify = channel_info.get('can_auto_verify', False)
                
                # Check if we already verified this user for this channel
                if self.user_channel_memberships[user_id].get(channel_key):
                    # Already verified via trust or auto-verify
                    if can_auto_verify and chat_id and isinstance(chat_id, int):
                        # Recheck to see if user left
                        try:
                            member = await self.bot.get_chat_member(
                                chat_id=chat_id,
                                user_id=user_id
                            )
                            if member.status not in ['member', 'administrator', 'creator']:
                                # User left, mark as not joined
                                self.user_channel_memberships[user_id][channel_key] = False
                                not_joined.append(channel_info)
                        except Exception as e:
                            logger.warning(f"Cannot recheck membership for {chat_id}: {e}")
                    continue
                
                # If bot is admin in channel, do automatic verification
                if can_auto_verify and chat_id and isinstance(chat_id, int):
                    try:
                        member = await self.bot.get_chat_member(
                            chat_id=chat_id,
                            user_id=user_id
                        )
                        if member.status in ['member', 'administrator', 'creator']:
                            # Mark as verified
                            self.user_channel_memberships[user_id][channel_key] = True
                            logger.info(f"User {user_id} verified automatically in {chat_id}")
                        else:
                            # Not joined or kicked
                            self.user_channel_memberships[user_id][channel_key] = False
                            not_joined.append(channel_info)
                    except Exception as e:
                        logger.warning(f"Cannot auto-check membership for {chat_id}: {e}")
                        # Cannot verify, add to not_joined
                        if not self.user_channel_memberships[user_id].get(channel_key):
                            not_joined.append(channel_info)
                else:
                    # Bot is not admin - trust-based after user confirms
                    if not self.user_channel_memberships[user_id].get(channel_key):
                        not_joined.append(channel_info)
                    
            except Exception as e:
                logger.error(f"Error checking membership for channel {channel_key}: {e}")
                if not self.user_channel_memberships[user_id].get(channel_key):
                    not_joined.append(channel_info)
        
        return len(not_joined) == 0, not_joined
    
    def mark_user_joined_channel(self, user_id: int, channel_key: str):
        """Mark that user has joined a channel (trust-based)"""
        if user_id not in self.user_channel_memberships:
            self.user_channel_memberships[user_id] = {}
        self.user_channel_memberships[user_id][channel_key] = True
        logger.info(f"User {user_id} marked as joined channel {channel_key} (trust-based)")
    
    def get_channel_url(self, channel_info: dict) -> str:
        """Convert channel info to a valid URL"""
        # Priority: invite_link > display URL > username
        if channel_info.get('invite_link'):
            return channel_info['invite_link']
        
        display = channel_info.get('display', '')
        
        # If it's already a URL, return it
        if display.startswith('http'):
            return display
        
        # If it's a username starting with @, convert to URL
        if display.startswith('@'):
            username = display[1:]  # Remove @
            return f"https://t.me/{username}"
        
        # Default: return as is
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
            
            # Send only redownload button
            keyboard = []
            if file_code:
                keyboard.append([InlineKeyboardButton("🔄 دریافت مجدد محتوا", callback_data=f"redownload_{file_code}")])
            
            await self.bot.send_message(
                chat_id=chat_id,
                text="محتوا پاک شد. می‌توانید دوباره دریافت کنید:",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        except Exception as e:
            logger.error(f"Error in deletion process: {e}")
    
    def check_spam(self, user_id: int) -> tuple[bool, int]:
        """Check if user is spamming"""
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
    
    async def handle_bot_chat_member(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle when bot is added to a chat or its status changes"""
        try:
            chat_member_update = update.my_chat_member
            
            if not chat_member_update:
                return
            
            chat = chat_member_update.chat
            new_status = chat_member_update.new_chat_member.status
            old_status = chat_member_update.old_chat_member.status
            
            # Check if bot became admin or was added as admin
            if new_status in ['administrator', 'creator'] and old_status not in ['administrator', 'creator']:
                # Bot just became admin!
                chat_id = chat.id
                chat_title = chat.title or chat.username or "Unknown"
                chat_type = chat.type
                
                # Get invite link if available
                try:
                    invite_link = await self.bot.export_chat_invite_link(chat_id=chat_id)
                    logger.info(f"Exported invite link for {chat_title}: {invite_link}")
                except Exception as e:
                    invite_link = None
                    logger.warning(f"Cannot export invite link: {e}")
                
                # Store detected channel
                self.detected_channels[chat_id] = {
                    'chat_id': chat_id,
                    'title': chat_title,
                    'type': chat_type,
                    'username': chat.username,
                    'invite_link': invite_link,
                    'display': f"@{chat.username}" if chat.username else invite_link or str(chat_id),
                    'detected_at': datetime.now(timezone.utc).isoformat()
                }
                
                logger.info(f"Bot became admin in {chat_title} (ID: {chat_id})")
                
                # Notify main admin
                try:
                    notification_text = (
                        f"🔔 بات در کانال/گروه جدید ادمین شد!\n\n"
                        f"📢 نام: {chat_title}\n"
                        f"🆔 Chat ID: {chat_id}\n"
                        f"📝 نوع: {chat_type}\n"
                    )
                    
                    if chat.username:
                        notification_text += f"👤 یوزرنیم: @{chat.username}\n"
                    
                    if invite_link:
                        notification_text += f"🔗 لینک: {invite_link}\n"
                    
                    notification_text += "\nآیا می‌خواهید این کانال را به جوین اجباری اضافه کنید?"
                    
                    keyboard = [
                        [InlineKeyboardButton("✅ بله، اضافه کن", callback_data=f"autoadd_{chat_id}")],
                        [InlineKeyboardButton("📋 فقط ذخیره کن", callback_data=f"autostore_{chat_id}")],
                        [InlineKeyboardButton("❌ نادیده بگیر", callback_data=f"autoignore_{chat_id}")]
                    ]
                    
                    await self.bot.send_message(
                        chat_id=MAIN_ADMIN_ID,
                        text=notification_text,
                        reply_markup=InlineKeyboardMarkup(keyboard)
                    )
                except Exception as e:
                    logger.error(f"Error notifying admin about new channel: {e}")
                    
        except Exception as e:
            logger.error(f"Error in handle_bot_chat_member: {e}")
    
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
            admin_text = (
                f"👋 سلام {user.first_name}!\n\n"
                f"✨ شما ادمین هستید. برای آپلود فایل، عکس یا ویدیو را ارسال کنید.\n\n"
                f"📝 می‌توانید چند فایل پشت سر هم ارسال کنید و یک لینک واحد دریافت کنید.\n\n"
                f"💬 برای پاسخ به پیام کاربران، روی پیام آن‌ها Reply کنید.\n\n"
                f"⚠️ توجه: بات بدون دیتابیس است. با restart، لینک‌ها و تنظیمات پاک می‌شوند!\n\n"
                f"از دکمه‌های زیر برای مدیریت بات استفاده کنید:"
            )
            
            # Add admin management button only for main admin
            if user.id == MAIN_ADMIN_ID:
                keyboard = [
                    [KeyboardButton("👥 کاربران"), KeyboardButton("📁 فایل‌ها")],
                    [KeyboardButton("📨 ارسال PM"), KeyboardButton("🔒 جوین اجباری")],
                    [KeyboardButton("👤 مدیریت ادمین‌ها")]
                ]
                admin_keyboard = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
            else:
                admin_keyboard = self.get_admin_keyboard()
            
            await update.message.reply_text(admin_text, reply_markup=admin_keyboard)
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
                channel_key = str(channel.get('chat_id') or channel.get('identifier'))
                channel_url = self.get_channel_url(channel)
                
                # Always use URL button (no callback) - direct link
                keyboard.append([InlineKeyboardButton(
                    channel['button_text'],
                    url=channel_url
                )])
            
            keyboard.append([InlineKeyboardButton(
                "✅ عضو شدم",
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
        
        # Handle auto-detected channels
        if data.startswith("autoadd_"):
            if user.id != MAIN_ADMIN_ID:
                await query.answer("❌ فقط ادمین اصلی دسترسی دارد.", show_alert=True)
                return
            
            chat_id = int(data.replace("autoadd_", ""))
            
            if chat_id not in self.detected_channels:
                await query.answer("❌ کانال پیدا نشد.", show_alert=True)
                return
            
            # Set up to add channel - ask for button text
            context.user_data['temp_channel_from_auto'] = self.detected_channels[chat_id]
            context.user_data['awaiting'] = 'auto_channel_button_text'
            
            channel_info = self.detected_channels[chat_id]
            await query.edit_message_text(
                f"✅ کانال انتخاب شد!\n\n"
                f"📢 نام: {channel_info['title']}\n"
                f"🆔 Chat ID: {chat_id}\n\n"
                f"📝 حالا متن دکمه را وارد کنید:\n\n"
                f"مثال: عضویت در کانال اصلی"
            )
            return
        
        elif data.startswith("autostore_"):
            if user.id != MAIN_ADMIN_ID:
                await query.answer("❌ فقط ادمین اصلی دسترسی دارد.", show_alert=True)
                return
            
            chat_id = int(data.replace("autostore_", ""))
            await query.answer("✅ کانال ذخیره شد!", show_alert=True)
            await query.edit_message_text(
                f"{query.message.text}\n\n"
                f"✅ کانال ذخیره شد. می‌توانید بعداً از منوی جوین اجباری آن را اضافه کنید."
            )
            return
        
        elif data.startswith("autoignore_"):
            if user.id != MAIN_ADMIN_ID:
                await query.answer("❌ فقط ادمین اصلی دسترسی دارد.", show_alert=True)
                return
            
            chat_id = int(data.replace("autoignore_", ""))
            
            if chat_id in self.detected_channels:
                del self.detected_channels[chat_id]
            
            await query.answer("✅ نادیده گرفته شد.", show_alert=True)
            await query.edit_message_text(f"{query.message.text}\n\n❌ نادیده گرفته شد.")
            return
        
        # Handle admin management - Only main admin can access
        if data == "add_new_admin":
            if user.id != MAIN_ADMIN_ID:
                await query.answer("❌ فقط ادمین اصلی دسترسی دارد.", show_alert=True)
                return
            
            context.user_data['awaiting'] = 'new_admin_id'
            keyboard = [[InlineKeyboardButton("❌ لغو", callback_data="cancel_user_send")]]
            await query.edit_message_text(
                "👤 لطفاً آیدی عددی کاربر را برای افزودن به عنوان ادمین ارسال کنید:",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
            return
        
        elif data.startswith("removeadmin_"):
            if user.id != MAIN_ADMIN_ID:
                await query.answer("❌ فقط ادمین اصلی دسترسی دارد.", show_alert=True)
                return
            
            admin_id_to_remove = int(data.replace("removeadmin_", ""))
            
            if admin_id_to_remove == MAIN_ADMIN_ID:
                await query.answer("❌ نمی‌توانید ادمین اصلی را حذف کنید.", show_alert=True)
                return
            
            if admin_id_to_remove in self.admins:
                del self.admins[admin_id_to_remove]
                await query.answer(f"✅ ادمین {admin_id_to_remove} حذف شد.", show_alert=True)
                
                # Refresh admin list
                admin_list = "👥 لیست ادمین‌های فعلی:\n\n"
                keyboard = []
                
                for admin_id in self.admins.keys():
                    if admin_id == MAIN_ADMIN_ID:
                        admin_list += f"• {admin_id} (ادمین اصلی) ⭐\n"
                    else:
                        admin_list += f"• {admin_id}\n"
                        keyboard.append([InlineKeyboardButton(f"🗑 حذف {admin_id}", callback_data=f"removeadmin_{admin_id}")])
                
                admin_list += "\n💡 برای افزودن ادمین جدید، از دکمه زیر استفاده کنید:"
                keyboard.append([InlineKeyboardButton("➕ افزودن ادمین جدید", callback_data="add_new_admin")])
                
                await query.edit_message_text(admin_list, reply_markup=InlineKeyboardMarkup(keyboard))
                logger.info(f"Admin removed: {admin_id_to_remove}")
            else:
                await query.answer("❌ این کاربر ادمین نیست.", show_alert=True)
            return
        
        elif data.startswith("delchan_"):
            if not self.is_admin(user.id):
                await query.answer("❌ فقط ادمین‌ها دسترسی دارند.", show_alert=True)
                return
            
            channel_key = data.replace("delchan_", "")
            
            if channel_key in self.mandatory_channels:
                removed_channel = self.mandatory_channels[channel_key]
                del self.mandatory_channels[channel_key]
                
                await query.answer(
                    f"✅ کانال حذف شد!\n{removed_channel.get('button_text', 'Unknown')}", 
                    show_alert=True
                )
                
                # Refresh channel list
                if not self.mandatory_channels:
                    await query.edit_message_text("✅ کانال حذف شد.\n\n📋 دیگر کانال اجباری وجود ندارد.")
                else:
                    message = f"📢 کانال‌های باقی‌مانده ({len(self.mandatory_channels)} عدد):\n\n"
                    keyboard = []
                    
                    for idx, (ch_key, ch_info) in enumerate(self.mandatory_channels.items(), 1):
                        message += f"{idx}. {ch_info['button_text']}\n"
                        message += f"   🔗 {ch_info['display']}\n\n"
                        keyboard.append([InlineKeyboardButton(f"🗑 حذف: {ch_info['button_text']}", callback_data=f"delchan_{ch_key}")])
                    
                    await query.edit_message_text(message, reply_markup=InlineKeyboardMarkup(keyboard))
                
                logger.info(f"Channel removed: {removed_channel.get('display')}, remaining: {len(self.mandatory_channels)}")
            else:
                await query.answer("❌ کانال پیدا نشد.", show_alert=True)
            return
        
        elif data.startswith("delfile_"):
            if not self.is_admin(user.id):
                await query.answer("❌ فقط ادمین‌ها دسترسی دارند.", show_alert=True)
                return
            
            file_code = data.replace("delfile_", "")
            
            if file_code in self.files:
                del self.files[file_code]
                await query.answer(f"✅ لینک فایل {file_code} حذف شد!", show_alert=True)
                
                # Refresh file list
                if not self.files:
                    await query.edit_message_text("✅ لینک فایل حذف شد.\n\n📋 دیگر لینک فایلی وجود ندارد.")
                else:
                    try:
                        bot_username = (await self.bot.get_me()).username
                        message = f"🗑 لیست لینک‌های باقی‌مانده ({len(self.files)} عدد):\n\n"
                        keyboard = []
                        
                        for idx, (code, file_info) in enumerate(self.files.items(), 1):
                            file_count = len(file_info.get('files', []))
                            caption = file_info.get('caption', 'بدون متن')
                            if len(caption) > 20:
                                caption = caption[:20] + "..."
                            
                            message += f"{idx}. {code} ({file_count} فایل)\n"
                            keyboard.append([InlineKeyboardButton(f"🗑 حذف: {code} - {caption}", callback_data=f"delfile_{code}")])
                            
                            if idx >= 15:
                                message += f"\n... و {len(self.files) - 15} لینک دیگر\n"
                                break
                        
                        await query.edit_message_text(message, reply_markup=InlineKeyboardMarkup(keyboard))
                    except Exception as e:
                        await query.edit_message_text("✅ لینک فایل حذف شد.")
                
                logger.info(f"File link {file_code} deleted by admin {user.id}")
            else:
                await query.answer("❌ لینک فایل پیدا نشد.", show_alert=True)
            return
        
        elif data.startswith("unblock_"):
            if not self.is_admin(user.id):
                await query.answer("❌ فقط ادمین‌ها دسترسی دارند.", show_alert=True)
                return
            
            user_id_to_unblock = int(data.replace("unblock_", ""))
            
            if user_id_to_unblock in self.users:
                self.users[user_id_to_unblock]['is_blocked'] = False
                self.users[user_id_to_unblock].pop('blocked_at', None)
                
                await query.answer(f"✅ کاربر {user_id_to_unblock} آنبلاک شد!", show_alert=True)
                
                # Refresh blocked users list
                blocked_users = [u for u in self.users.values() if u.get('is_blocked', False)]
                
                if not blocked_users:
                    await query.edit_message_text("✅ کاربر آنبلاک شد.\n\n📋 دیگر کاربر بلاک شده‌ای وجود ندارد.")
                else:
                    message = f"🚫 کاربران بلاک شده باقی‌مانده ({len(blocked_users)} نفر):\n\n"
                    keyboard = []
                    
                    for u in blocked_users[:20]:
                        username_display = f"@{u.get('username', 'ندارد')}"
                        message += f"• {u.get('first_name', 'Unknown')} ({username_display}) - ID: {u['user_id']}\n"
                        keyboard.append([InlineKeyboardButton(
                            f"✅ آنبلاک: {u.get('first_name', 'Unknown')} ({u['user_id']})", 
                            callback_data=f"unblock_{u['user_id']}"
                        )])
                    
                    if len(blocked_users) > 20:
                        message += f"\n... و {len(blocked_users) - 20} نفر دیگر"
                    
                    await query.edit_message_text(message, reply_markup=InlineKeyboardMarkup(keyboard))
                
                logger.info(f"User unblocked: {user_id_to_unblock}")
            else:
                await query.answer("❌ کاربر پیدا نشد.", show_alert=True)
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
                "🗑 آپلود لغو شد و همه فایل‌ها پاک شدند."
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
                "یا می‌توانید از دکمه زیر برای ارتباط با مدیر استفاده کنید:"
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
                "⏳ لطفاً منتظر پاسخ ادمین باشید."
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
                    channel_key = str(channel.get('chat_id') or channel.get('identifier'))
                    channel_url = self.get_channel_url(channel)
                    
                    # Always use URL button - direct link
                    keyboard.append([InlineKeyboardButton(
                        channel['button_text'],
                        url=channel_url
                    )])
                
                keyboard.append([InlineKeyboardButton(
                    "✅ عضو شدم",
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
            
            # Check membership again - with improved logic
            is_member, not_joined_channels = await self.check_membership(user.id)
            
            if not_joined_channels:
                # Separate auto-verify (bot is admin) vs trust-based (bot not admin)
                auto_verify_failed = []
                trust_based_channels = []
                
                for channel in not_joined_channels:
                    channel_key = str(channel.get('chat_id') or channel.get('identifier'))
                    if channel.get('can_auto_verify'):
                        # Bot IS admin - auto verification failed
                        auto_verify_failed.append(channel)
                    else:
                        # Bot is NOT admin - trust user but warn them
                        trust_based_channels.append(channel)
                        # Mark as joined (trust-based)
                        self.mark_user_joined_channel(user.id, channel_key)
                
                # If there are auto-verify failures, user MUST join
                if auto_verify_failed:
                    # Build list of channels not joined
                    channel_names = "\n".join([f"• {ch['button_text']}" for ch in auto_verify_failed])
                    await query.answer(
                        f"❌ شما هنوز در این کانال‌ها عضو نیستید:\n\n{channel_names}\n\n"
                        "لطفاً ابتدا عضو شوید و سپس دوباره «عضو شدم ✅» را بزنید.",
                        show_alert=True
                    )
                    logger.info(f"User {user.id} failed auto-verify for {len(auto_verify_failed)} channels")
                    return
                
                # If only trust-based channels remain, show warning then allow
                if trust_based_channels:
                    channel_names = "\n".join([f"• {ch['button_text']}" for ch in trust_based_channels])
                    await query.answer(
                        f"✅ عضویت شما تایید شد!\n\n"
                        f"⚠️ توجه: لطفاً مطمئن شوید در این کانال‌ها عضو هستید:\n{channel_names}",
                        show_alert=True
                    )
                    logger.info(f"User {user.id} verified via trust for {len(trust_based_channels)} channels")
                
                is_member = True
            
            if file_code not in self.files:
                await query.answer("❌ این لینک وجود ندارد.", show_alert=True)
                return
            
            # Send files
            await self.send_files_to_user(user.id, self.files[file_code], file_code)
            await query.answer("✅ در حال ارسال فایل‌ها...", show_alert=False)
            
            # Update message
            try:
                await query.edit_message_text("✅ فایل‌ها ارسال شدند! لطفاً پیام‌های بالا را چک کنید.")
            except:
                pass
            
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
        
        # Handle keyboard button presses
        if text == "📞 ارتباط با مدیر":
            context.user_data['awaiting'] = 'user_content_to_admin'
            keyboard = [[InlineKeyboardButton("❌ لغو", callback_data="cancel_user_send")]]
            await update.message.reply_text(
                "📞 ارتباط با مدیر\n\n"
                "لطفاً پیام، عکس یا ویدیوی خود را ارسال کنید:",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
            return
        
        # Admin keyboard buttons
        if self.is_admin(user.id):
            # Users menu
            if text == "👥 کاربران":
                keyboard = [
                    [InlineKeyboardButton("👥 کاربران فعال", callback_data="menu_active_users")],
                    [InlineKeyboardButton("🔨 بلاک کاربر", callback_data="menu_block_user")],
                    [InlineKeyboardButton("✅ آنبلاک کاربر", callback_data="menu_unblock_user")]
                ]
                await update.message.reply_text(
                    "👥 منوی مدیریت کاربران:\n\n"
                    "لطفاً یکی از گزینه‌های زیر را انتخاب کنید:",
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )
                return
            
            # Files menu
            elif text == "📁 فایل‌ها":
                keyboard = [
                    [InlineKeyboardButton("📋 لیست فایل‌ها", callback_data="menu_list_files")],
                    [InlineKeyboardButton("🗑 حذف لینک فایل", callback_data="menu_delete_file")]
                ]
                await update.message.reply_text(
                    "📁 منوی مدیریت فایل‌ها:\n\n"
                    "لطفاً یکی از گزینه‌های زیر را انتخاب کنید:",
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )
                return
            
            # PM menu
            elif text == "📨 ارسال PM":
                keyboard = [
                    [InlineKeyboardButton("📤 ارسال همگانی", callback_data="menu_broadcast")],
                    [InlineKeyboardButton("📩 پیام به کاربر", callback_data="menu_pm_user")]
                ]
                await update.message.reply_text(
                    "📨 منوی ارسال پیام:\n\n"
                    "لطفاً یکی از گزینه‌های زیر را انتخاب کنید:",
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )
                return
            
            # Force join menu
            elif text == "🔒 جوین اجباری":
                keyboard = [
                    [InlineKeyboardButton("📢 کانال‌های اجباری", callback_data="menu_list_channels")],
                    [InlineKeyboardButton("➕ افزودن کانال", callback_data="menu_add_channel")],
                    [InlineKeyboardButton("➖ حذف کانال", callback_data="menu_remove_channel")]
                ]
                
                # Add detected channels button if any
                if self.detected_channels:
                    keyboard.insert(1, [InlineKeyboardButton(
                        f"🔍 کانال‌های شناسایی شده ({len(self.detected_channels)})",
                        callback_data="menu_detected_channels"
                    )])
                
                await update.message.reply_text(
                    "🔒 منوی جوین اجباری:\n\n"
                    "لطفاً یکی از گزینه‌های زیر را انتخاب کنید:",
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )
                return
            
            # Admin management - Only for main admin
            elif text == "👤 مدیریت ادمین‌ها":
                if user.id != MAIN_ADMIN_ID:
                    await update.message.reply_text("❌ فقط ادمین اصلی دسترسی دارد.")
                    return
                
                admin_list = "👥 لیست ادمین‌های فعلی:\n\n"
                keyboard = []
                
                for admin_id in self.admins.keys():
                    if admin_id == MAIN_ADMIN_ID:
                        admin_list += f"• {admin_id} (ادمین اصلی) ⭐\n"
                    else:
                        admin_list += f"• {admin_id}\n"
                        # Add remove button for non-main admins
                        keyboard.append([InlineKeyboardButton(f"🗑 حذف {admin_id}", callback_data=f"removeadmin_{admin_id}")])
                
                admin_list += "\n💡 برای افزودن ادمین جدید، از دکمه زیر استفاده کنید:"
                keyboard.append([InlineKeyboardButton("➕ افزودن ادمین جدید", callback_data="add_new_admin")])
                
                await update.message.reply_text(admin_list, reply_markup=InlineKeyboardMarkup(keyboard))
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
                "✅ پیام شما با موفقیت برای ادمین ارسال شد!\n\n"
                "⏳ لطفاً منتظر پاسخ ادمین باشید.",
                reply_markup=self.get_user_keyboard()
            )
            
            context.user_data.clear()
            return
        
        elif awaiting == 'caption_for_files':
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
            if 'temp_files' not in context.user_data or not context.user_data['temp_files']:
                await update.message.reply_text("❌ خطا: فایلی یافت نشد.")
                context.user_data.clear()
                return
            
            try:
                delete_seconds = int(text)
                if delete_seconds < 5 or delete_seconds > 30:
                    await update.message.reply_text("❌ لطفاً عددی بین 5 تا 30 وارد کنید.")
                    return
            except ValueError:
                await update.message.reply_text("❌ لطفاً یک عدد معتبر وارد کنید.")
                return
            
            # Generate unique code
            unique_code = secrets.token_urlsafe(6)
            
            # Save file group
            self.files[unique_code] = {
                'files': context.user_data['temp_files'],
                'caption': context.user_data.get('caption'),
                'delete_seconds': delete_seconds,
                'created_at': datetime.now(timezone.utc).isoformat(),
                'admin_id': user.id
            }
            
            bot_username = (await self.bot.get_me()).username
            share_link = f"https://t.me/{bot_username}?start={unique_code}"
            
            await update.message.reply_text(
                f"✅ لینک فایل با موفقیت ساخته شد!\n\n"
                f"🔗 لینک: {share_link}\n\n"
                f"📦 تعداد فایل‌ها: {len(context.user_data['temp_files'])}\n"
                f"📝 متن: {context.user_data.get('caption', 'بدون متن')}\n"
                f"⏱️ زمان حذف: {delete_seconds} ثانیه\n\n"
                f"این لینک را برای کاربران ارسال کنید."
            )
            
            context.user_data.clear()
            logger.info(f"File link created: {unique_code} by admin {user.id}")
            return
        
        elif awaiting == 'channel_link':
            if not self.is_admin(user.id):
                return
            
            channel_info = self.extract_channel_info(text)
            
            if not channel_info:
                await update.message.reply_text("❌ فرمت نامعتبر! لطفاً دوباره تلاش کنید.")
                return
            
            # Check if bot is admin and get actual chat_id + invite_link
            is_admin, chat_id, invite_link = await self.check_if_bot_is_admin(channel_info['identifier'])
            channel_info['can_auto_verify'] = is_admin
            if chat_id:
                channel_info['chat_id'] = chat_id
            if invite_link:
                channel_info['invite_link'] = invite_link
            
            context.user_data['temp_channel'] = channel_info
            context.user_data['awaiting'] = 'channel_button_text'
            
            verify_status = "✅ بات ادمین است (چک خودکار)" if is_admin else "⚠️ بات ادمین نیست (بر اساس اعتماد)"
            
            response_text = (
                f"✅ کانال شناسایی شد!\n\n"
                f"🔗 {channel_info['display']}\n"
                f"🔍 {verify_status}\n"
            )
            
            if invite_link:
                response_text += f"📎 لینک دعوت: {invite_link}\n"
            
            response_text += f"\n📝 حالا متن دکمه را وارد کنید:\n\nمثال: عضویت در کانال اصلی"
            
            await update.message.reply_text(response_text)
            return
        
        elif awaiting == 'channel_button_text':
            if not self.is_admin(user.id):
                return
            
            if 'temp_channel' not in context.user_data:
                await update.message.reply_text("❌ خطا: اطلاعات کانال یافت نشد.")
                context.user_data.clear()
                return
            
            channel_info = context.user_data['temp_channel']
            channel_info['button_text'] = text
            
            # Save channel - use chat_id as key if available, otherwise identifier
            channel_key = str(channel_info.get('chat_id') or channel_info['identifier'])
            self.mandatory_channels[channel_key] = channel_info
            
            verify_status = "✅ چک خودکار" if channel_info.get('can_auto_verify') else "🤝 بر اساس اعتماد"
            
            response_text = (
                f"✅ کانال با موفقیت اضافه شد!\n\n"
                f"📢 متن دکمه: {text}\n"
                f"🔗 لینک: {channel_info['display']}\n"
            )
            
            if channel_info.get('invite_link'):
                response_text += f"📎 لینک دعوت: {channel_info['invite_link']}\n"
            
            response_text += f"🔍 حالت: {verify_status}\n\nتعداد کانال‌های اجباری: {len(self.mandatory_channels)}"
            
            await update.message.reply_text(response_text)
            
            context.user_data.clear()
            logger.info(f"Channel added: {channel_info['display']}")
            return
        
        elif awaiting == 'auto_channel_button_text':
            if user.id != MAIN_ADMIN_ID:
                return
            
            if 'temp_channel_from_auto' not in context.user_data:
                await update.message.reply_text("❌ خطا: اطلاعات کانال یافت نشد.")
                context.user_data.clear()
                return
            
            channel_info = context.user_data['temp_channel_from_auto']
            channel_info['button_text'] = text
            channel_info['can_auto_verify'] = True  # Auto-detected channels are always admin
            
            # Save to mandatory channels
            channel_key = str(channel_info['chat_id'])
            self.mandatory_channels[channel_key] = channel_info
            
            response_text = (
                f"✅ کانال با موفقیت به جوین اجباری اضافه شد!\n\n"
                f"📢 نام: {channel_info['title']}\n"
                f"📝 متن دکمه: {text}\n"
                f"🆔 Chat ID: {channel_info['chat_id']}\n"
            )
            
            if channel_info.get('invite_link'):
                response_text += f"📎 لینک دعوت: {channel_info['invite_link']}\n"
            
            response_text += f"🔍 حالت: چک خودکار ✅\n\nتعداد کانال‌های اجباری: {len(self.mandatory_channels)}"
            
            await update.message.reply_text(response_text)
            
            context.user_data.clear()
            logger.info(f"Auto-detected channel added to mandatory: {channel_info['title']}")
            return
        
        elif awaiting == 'target_user_id':
            if not self.is_admin(user.id):
                return
            
            try:
                target_user_id = int(text)
            except ValueError:
                await update.message.reply_text("❌ لطفاً یک آیدی عددی معتبر وارد کنید.")
                return
            
            if target_user_id not in self.users:
                await update.message.reply_text("❌ این کاربر یافت نشد.")
                return
            
            context.user_data['target_user_id'] = target_user_id
            context.user_data['awaiting'] = 'pm_message'
            
            target_user = self.users[target_user_id]
            await update.message.reply_text(
                f"✅ کاربر پیدا شد!\n\n"
                f"👤 نام: {target_user.get('first_name', 'Unknown')}\n"
                f"🆔 آیدی: {target_user_id}\n\n"
                f"📝 حالا پیام خود را ارسال کنید:"
            )
            return
        
        elif awaiting == 'pm_message':
            if not self.is_admin(user.id):
                return
            
            if 'target_user_id' not in context.user_data:
                await update.message.reply_text("❌ خطا: کاربر هدف یافت نشد.")
                context.user_data.clear()
                return
            
            target_user_id = context.user_data['target_user_id']
            
            try:
                await self.bot.send_message(
                    chat_id=target_user_id,
                    text=f"💬 پیام از ادمین:\n\n{text}"
                )
                await update.message.reply_text("✅ پیام با موفقیت ارسال شد!")
            except Exception as e:
                logger.error(f"Error sending PM: {e}")
                await update.message.reply_text("❌ خطا در ارسال پیام.")
            
            context.user_data.clear()
            return
        
        elif awaiting == 'block_user_id':
            if not self.is_admin(user.id):
                return
            
            try:
                user_id_to_block = int(text)
            except ValueError:
                await update.message.reply_text("❌ لطفاً یک آیدی عددی معتبر وارد کنید.")
                return
            
            if user_id_to_block not in self.users:
                await update.message.reply_text("❌ این کاربر یافت نشد.")
                return
            
            self.users[user_id_to_block]['is_blocked'] = True
            self.users[user_id_to_block]['blocked_at'] = datetime.now(timezone.utc).isoformat()
            
            await update.message.reply_text(
                f"✅ کاربر {user_id_to_block} بلاک شد!\n\n"
                f"👤 نام: {self.users[user_id_to_block].get('first_name', 'Unknown')}"
            )
            
            context.user_data.clear()
            logger.info(f"User blocked: {user_id_to_block}")
            return
        
        elif awaiting == 'new_admin_id':
            if user.id != MAIN_ADMIN_ID:
                return
            
            try:
                new_admin_id = int(text)
            except ValueError:
                await update.message.reply_text("❌ لطفاً یک آیدی عددی معتبر وارد کنید.")
                return
            
            if new_admin_id in self.admins:
                await update.message.reply_text("❌ این کاربر قبلاً ادمین است.")
                return
            
            self.admins[new_admin_id] = {
                'added_at': datetime.now(timezone.utc).isoformat(),
                'added_by': user.id
            }
            
            await update.message.reply_text(
                f"✅ کاربر {new_admin_id} به عنوان ادمین اضافه شد!\n\n"
                f"تعداد ادمین‌ها: {len(self.admins)}"
            )
            
            context.user_data.clear()
            logger.info(f"New admin added: {new_admin_id} by {user.id}")
            return
    
    async def handle_inline_menu_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle inline menu callbacks"""
        query = update.callback_query
        await query.answer()
        
        user = update.effective_user
        data = query.data
        
        if not self.is_admin(user.id):
            await query.answer("❌ فقط ادمین‌ها دسترسی دارند.", show_alert=True)
            return
        
        # Users menu
        if data == "menu_active_users":
            active_users = [u for u in self.users.values() if not u.get('is_blocked', False)]
            
            if not active_users:
                await query.edit_message_text("📋 هیچ کاربر فعالی وجود ندارد.")
                return
            
            message = f"👥 کاربران فعال ({len(active_users)} نفر):\n\n"
            for u in active_users[:30]:
                message += f"• {u.get('first_name', 'Unknown')} (@{u.get('username', 'none')}) - ID: {u['user_id']}\n"
            
            if len(active_users) > 30:
                message += f"\n... و {len(active_users) - 30} نفر دیگر"
            
            await query.edit_message_text(message)
            return
        
        elif data == "menu_block_user":
            context.user_data['awaiting'] = 'block_user_id'
            keyboard = [[InlineKeyboardButton("❌ لغو", callback_data="cancel_user_send")]]
            await query.edit_message_text(
                "🔨 لطفاً آیدی عددی کاربر برای بلاک کردن را ارسال کنید:",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
            return
        
        elif data == "menu_unblock_user":
            blocked_users = [u for u in self.users.values() if u.get('is_blocked', False)]
            
            if not blocked_users:
                await query.edit_message_text("📋 هیچ کاربر بلاک شده‌ای وجود ندارد.")
                return
            
            message = f"🚫 کاربران بلاک شده ({len(blocked_users)} نفر):\n\n"
            keyboard = []
            
            for u in blocked_users[:20]:
                username_display = f"@{u.get('username', 'ندارد')}"
                message += f"• {u.get('first_name', 'Unknown')} ({username_display}) - ID: {u['user_id']}\n"
                keyboard.append([InlineKeyboardButton(
                    f"✅ آنبلاک: {u.get('first_name', 'Unknown')} ({u['user_id']})", 
                    callback_data=f"unblock_{u['user_id']}"
                )])
            
            if len(blocked_users) > 20:
                message += f"\n... و {len(blocked_users) - 20} نفر دیگر"
            
            message += "\n\n👇 روی دکمه کاربر مورد نظر کلیک کنید:"
            
            await query.edit_message_text(message, reply_markup=InlineKeyboardMarkup(keyboard))
            return
        
        # Files menu
        elif data == "menu_list_files":
            if not self.files:
                await query.edit_message_text("📋 هیچ لینک فایلی وجود ندارد.")
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
                
                # Send first part as edit, rest as new messages
                await query.edit_message_text(message_parts[0])
                
                for part in message_parts[1:]:
                    await self.bot.send_message(chat_id=user.id, text=part)
                    
            except Exception as e:
                logger.error(f"Error in list_files: {e}")
                await query.edit_message_text("❌ خطا در نمایش لیست فایل‌ها.")
            return
        
        elif data == "menu_delete_file":
            if not self.files:
                await query.edit_message_text("📋 هیچ لینک فایلی برای حذف وجود ندارد.")
                return
            
            try:
                bot_username = (await self.bot.get_me()).username
                message = f"🗑 لیست لینک‌های فایل ({len(self.files)} عدد):\n\n"
                keyboard = []
                
                for idx, (code, file_info) in enumerate(self.files.items(), 1):
                    file_count = len(file_info.get('files', []))
                    caption = file_info.get('caption', 'بدون متن')
                    if len(caption) > 20:
                        caption = caption[:20] + "..."
                    
                    message += f"{idx}. {code} ({file_count} فایل)\n"
                    keyboard.append([InlineKeyboardButton(f"🗑 حذف: {code} - {caption}", callback_data=f"delfile_{code}")])
                    
                    if idx >= 15:
                        message += f"\n... و {len(self.files) - 15} لینک دیگر\n"
                        break
                
                message += "\n👇 روی دکمه لینک مورد نظر کلیک کنید:"
                
                await query.edit_message_text(message, reply_markup=InlineKeyboardMarkup(keyboard))
            except Exception as e:
                logger.error(f"Error in delete file menu: {e}")
                await query.edit_message_text("❌ خطا در نمایش لیست فایل‌ها.")
            return
        
        # PM menu
        elif data == "menu_broadcast":
            context.user_data['awaiting'] = 'broadcast_message'
            keyboard = [[InlineKeyboardButton("❌ لغو", callback_data="cancel_user_send")]]
            await query.edit_message_text(
                "📢 لطفاً پیامی که می‌خواهید به همه کاربران ارسال شود را بنویسید:",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
            return
        
        elif data == "menu_pm_user":
            context.user_data['awaiting'] = 'target_user_id'
            keyboard = [[InlineKeyboardButton("❌ لغو", callback_data="cancel_user_send")]]
            await query.edit_message_text(
                "📩 لطفاً آیدی عددی کاربر را وارد کنید:",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
            return
        
        # Force join menu
        elif data == "menu_list_channels":
            if not self.mandatory_channels:
                await query.edit_message_text("📋 هیچ کانال اجباری تنظیم نشده است.")
                return
            
            message = f"📢 کانال‌های عضویت اجباری ({len(self.mandatory_channels)} عدد):\n\n"
            for idx, (ch_key, ch_info) in enumerate(self.mandatory_channels.items(), 1):
                verify_mode = "✅ چک خودکار" if ch_info.get('can_auto_verify') else "🤝 بر اساس اعتماد"
                message += f"{idx}. {ch_info['button_text']}\n"
                message += f"   🔗 {ch_info['display']}\n"
                if ch_info.get('invite_link'):
                    message += f"   📎 {ch_info['invite_link']}\n"
                message += f"   🔍 {verify_mode}\n\n"
            
            await query.edit_message_text(message)
            return
        
        elif data == "menu_add_channel":
            context.user_data['awaiting'] = 'channel_link'
            keyboard = [[InlineKeyboardButton("❌ لغو", callback_data="cancel_user_send")]]
            await query.edit_message_text(
                "📢 لینک یا یوزرنیم یا Chat ID کانال را ارسال کنید\n\n"
                "✅ فرمت‌های قابل قبول:\n"
                "• @channelname\n"
                "• https://t.me/channelname\n"
                "• https://t.me/+ZtfIKEcLcoM0ZThl (لینک خصوصی)\n"
                "• -1001234567890 (Chat ID)\n\n"
                "💡 نکته: بات خودکار تشخیص می‌دهد که ادمین است یا نه.\n"
                "اگر ادمین باشد، لینک دعوت رو می‌گیره و چک خودکار فعاله.\n"
                "اگر ادمین نباشد، جوین بر اساس اعتماد به کاربر خواهد بود.",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
            return
        
        elif data == "menu_remove_channel":
            if not self.mandatory_channels:
                await query.edit_message_text("📋 هیچ کانال اجباری وجود ندارد.")
                return
            
            message = "📢 لیست کانال‌ها:\n\n"
            keyboard = []
            
            for idx, (ch_key, ch_info) in enumerate(self.mandatory_channels.items(), 1):
                message += f"{idx}. {ch_info['button_text']}\n"
                message += f"   🔗 {ch_info['display']}\n\n"
                keyboard.append([InlineKeyboardButton(f"🗑 حذف: {ch_info['button_text']}", callback_data=f"delchan_{ch_key}")])
            
            message += "👇 روی دکمه کانال مورد نظر کلیک کنید:"
            
            await query.edit_message_text(message, reply_markup=InlineKeyboardMarkup(keyboard))
            return
        
        elif data == "menu_detected_channels":
            if not self.detected_channels:
                await query.edit_message_text("📋 هیچ کانال شناسایی شده‌ای وجود ندارد.")
                return
            
            message = f"🔍 کانال‌های شناسایی شده ({len(self.detected_channels)} عدد):\n\n"
            message += "این کانال‌هایی هستند که بات در آن‌ها ادمین شده است:\n\n"
            
            keyboard = []
            for chat_id, ch_info in self.detected_channels.items():
                message += f"📢 {ch_info['title']}\n"
                message += f"   🆔 Chat ID: {chat_id}\n"
                if ch_info.get('username'):
                    message += f"   👤 @{ch_info['username']}\n"
                if ch_info.get('invite_link'):
                    message += f"   📎 {ch_info['invite_link']}\n"
                message += f"   📅 {ch_info['detected_at'][:10]}\n\n"
                
                # Check if already added to mandatory
                is_added = str(chat_id) in self.mandatory_channels
                if is_added:
                    keyboard.append([InlineKeyboardButton(
                        f"✅ {ch_info['title']} (اضافه شده)",
                        callback_data="noop"
                    )])
                else:
                    keyboard.append([InlineKeyboardButton(
                        f"➕ افزودن: {ch_info['title']}",
                        callback_data=f"autoadd_{chat_id}"
                    )])
            
            await query.edit_message_text(message, reply_markup=InlineKeyboardMarkup(keyboard))
            return
    
    def run(self):
        """Start the bot"""
        # Add handlers in correct order
        self.application.add_handler(CommandHandler("start", self.start_command))
        self.application.add_handler(ChatMemberHandler(self.handle_bot_chat_member, ChatMemberHandler.MY_CHAT_MEMBER))
        self.application.add_handler(MessageHandler(filters.PHOTO | filters.VIDEO, self.handle_media))
        self.application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_text))
        self.application.add_handler(CallbackQueryHandler(self.handle_inline_menu_callback, pattern="^menu_"))
        self.application.add_handler(CallbackQueryHandler(self.button_callback))
        
        logger.info("Bot started successfully!")
        logger.info(f"Main Admin ID: {MAIN_ADMIN_ID}")
        logger.info("✨ Auto-detection feature enabled!")
        logger.info("🔧 Fixed issues: Private channel links now work with invite_link export")
        
        # Run the bot
        self.application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    if not BOT_TOKEN:
        print("❌ Error: BOT_TOKEN not found in environment variables!")
        exit(1)
    
    if not MAIN_ADMIN_ID or MAIN_ADMIN_ID == 0:
        print("❌ Error: MAIN_ADMIN_ID not found in environment variables!")
        exit(1)
    
    bot = TelegramBot()
    bot.run()
