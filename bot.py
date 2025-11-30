import os
import discord
import asyncio
import logging
import time
from discord import app_commands
from discord.ext import commands, voice_recv, tasks
from openai import OpenAI
from dotenv import load_dotenv
from cfg import DISCORD_TOKEN, OPENAI_API_KEY
# 1. SETUP
logging.getLogger("discord.ext.voice_recv.reader").setLevel(logging.ERROR)
logging.getLogger("discord.ext.voice_recv.gateway").setLevel(logging.ERROR)

load_dotenv()

TRIGGER_WORD = "computer"

openai_client = OpenAI(api_key=OPENAI_API_KEY)
intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True

class VoiceBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="/", intents=intents)

    async def setup_hook(self):
        await self.tree.sync()
        # Start the background silence checker
        self.check_silence.start()
        print("✅ Slash commands synced & Silence detector started!")

    @tasks.loop(seconds=0.5)
    async def check_silence(self):
        # Go through all active voice connections
        for vc in self.voice_clients:
            if isinstance(vc, voice_recv.VoiceRecvClient) and vc.is_listening():
                sink = vc._reader.sink
                if isinstance(sink, BasicAudioSink):
                    # Check every user we have data for
                    current_time = time.time()
                    # We copy items to avoid "dictionary changed size during iteration"
                    for user, last_time in list(sink.last_spoken.items()):
                        # If user has been silent for > 1.2 seconds, process!
                        if current_time - last_time > 1.2:
                            # Remove from tracker so we don't process twice
                            del sink.last_spoken[user]
                            audio_data = sink.get_audio(user)
                            if audio_data:
                                # Send to text channel
                                dest_channel = user.guild.text_channels[0]
                                asyncio.run_coroutine_threadsafe(
                                    process_audio(user, audio_data, vc, dest_channel),
                                    self.loop
                                )

bot = VoiceBot()

# 2. AUDIO SINK (With Timestamp Tracking)
class BasicAudioSink(voice_recv.AudioSink):
    def __init__(self):
        self.audio_data = {}
        self.last_spoken = {} # Tracks when the user last sent a packet
        self.packet_count = 0 

    def wants_opus(self):
        return False

    def write(self, user, data):
        self.packet_count += 1
        if self.packet_count % 50 == 0:
            print(".", end="", flush=True)

        if user not in self.audio_data:
            self.audio_data[user] = bytearray()
        
        self.audio_data[user] += data.pcm
        # Update the "last seen" timestamp
        self.last_spoken[user] = time.time()

    def get_audio(self, user):
        return self.audio_data.pop(user, None)

    def cleanup(self):
        pass

async def process_audio(user, audio_data, voice_client, channel):
    print(f"\n✂️ Silence detected! Processing audio for {user.display_name}...")
    
    pcm_file = f"{user.id}.pcm"
    wav_file = f"{user.id}.wav"
    
    with open(pcm_file, "wb") as f: 
        f.write(audio_data)

    os.system(f"ffmpeg -f s16le -ar 48000 -ac 2 -i {pcm_file} {wav_file} -y > /dev/null 2>&1")
    
    try:
        if os.path.getsize(wav_file) < 20000: # ~1 second of audio
            print("⚠️ Audio too short/noise, ignoring.")
            return

        print("📤 Sending to OpenAI...")
        with open(wav_file, "rb") as f:
            transcript = openai_client.audio.transcriptions.create(
                model="whisper-1", file=f
            ).text.lower()
        
        print(f"📝 Heard: '{transcript}'")

        if TRIGGER_WORD in transcript:
            query = transcript.replace(TRIGGER_WORD, "").strip()
            if not query: return

            await channel.send(f"**Heard:** {query}")
            
            response = openai_client.chat.completions.create(
                model="gpt-3.5-turbo",
                messages=[{"role": "user", "content": query}]
            )
            reply = response.choices[0].message.content
            print(f"🤖 Reply: {reply}")
            
            tts_file = "reply.mp3"
            openai_client.audio.speech.create(
                model="tts-1", voice="alloy", input=reply
            ).stream_to_file(tts_file)

            if voice_client.is_playing(): voice_client.stop()
            voice_client.play(discord.FFmpegPCMAudio(tts_file))

    except Exception as e:
        print(f"❌ Error: {e}")

    if os.path.exists(pcm_file): os.remove(pcm_file)
    if os.path.exists(wav_file): os.remove(wav_file)

# 3. COMMANDS
@bot.tree.command(name="join")
async def join(interaction: discord.Interaction):
    if interaction.user.voice:
        channel = interaction.user.voice.channel
        await interaction.response.send_message(f"Listening in {channel.name}...")
        vc = await channel.connect(cls=voice_recv.VoiceRecvClient)
        vc.listen(BasicAudioSink())
    else:
        await interaction.response.send_message("Join a voice channel first!")

@bot.tree.command(name="leave")
async def leave(interaction: discord.Interaction):
    if interaction.guild.voice_client:
        await interaction.guild.voice_client.disconnect()
        await interaction.response.send_message("Bye!")

@bot.event
async def on_ready():
    print(f'Logged in as {bot.user}')

bot.run(DISCORD_TOKEN)
