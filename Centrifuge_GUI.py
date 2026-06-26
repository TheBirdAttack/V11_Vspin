# Centrifuge_GUI.py
import tkinter as tk
import asyncio
import threading
import sys
import time
from v11_driver import BlindVSpinBackend, SPIN_DURATION

# --- 1. Async Setup ---
async_loop = asyncio.new_event_loop()
def start_background_loop(loop):
    asyncio.set_event_loop(loop)
    loop.run_forever()
threading.Thread(target=start_background_loop, args=(async_loop,), daemon=True).start()

# --- 2. Initialisierung ---
driver = BlindVSpinBackend(device_id="FT9DGFGQ") #Blind
is_connected = False
total_duration = 10  

# --- 3. Hauptfenster & VOLLBILD-FIX ---
root = tk.Tk()
root.title("Centrifuge Kiosk")

root.overrideredirect(True) 
root.attributes("-fullscreen", True)

screen_width = root.winfo_screenwidth()
screen_height = root.winfo_screenheight()
root.geometry(f"{screen_width}x{screen_height}+0+0")
root.configure(bg="white")



# --- 4. GUI Logik ---
timer_running = False
seconds_elapsed = 0

def update_status_ui(connected_status):
    global is_connected
    is_connected = connected_status
    color = "lime green" if is_connected else "red"
    status_canvas.itemconfig(status_light, fill=color)
    btn_connect.config(text="CONNECTED" if is_connected else "CONNECT")

def set_connect_wait_ui():
    btn_connect.config(text="WAIT...", state="disabled")
    status_canvas.itemconfig(status_light, fill="orange")

async def disconnect_task():
    global is_connected
    root.after(0, lambda: btn_connect.config(text="DISCONNECTING...", state="disabled"))
    try:
        # Tür schließen beim Disconnect
        await driver.close_door()
        # Falls dein Treiber eine Trenn-Funktion hat (z.B. await driver.disconnect()), hier ergänzen
    except Exception as e:
        print(f"[GUI FEHLER] Fehler beim Trennen: {e}")
    finally:
        root.after(0, update_status_ui, False)
        root.after(0, lambda: btn_connect.config(state="normal", text="CONNECT"))

async def shutdown_task():
    print("\n[GUI] Beende Anwendung. Schließe Tür...")
    if is_connected:
        try:
            await driver.close_door()
        except Exception as e:
            print(f"[GUI FEHLER] Konnte Tür beim Beenden nicht schließen: {e}")
    
    # Nachdem die Tür zu ist (oder falls nicht verbunden), Tkinter sicher beenden
    root.after(0, _quit_app)

def _quit_app():
    root.destroy()
    sys.exit(0)

def on_exit(event=None):
    """Wird bei ESC oder EXIT-Button aufgerufen"""
    btn_exit.config(text="CLOSING...", state="disabled", bg="gray")
    # Den asynchronen Shutdown in den Background-Loop schicken
    asyncio.run_coroutine_threadsafe(shutdown_task(), async_loop)




# --- Live Zeit- und RPM-Anzeige während Spin ---
spin_running = False
spin_start_time = 0


def update_live_spin():
    """
    Aktualisiert die GUI-Anzeige (Timer & RPM) unabhängig von der Treiber-Latenz.
    """
    if not spin_running:
        return

    # Zeitberechnung basierend auf der tatsächlichen Startzeit (Echtzeit)
    elapsed = int(time.time() - spin_start_time)
    
    # Timer-Label aktualisieren
    mins, secs = divmod(elapsed, 60)
    timer_label.config(text=f"{mins:02d}:{secs:02d}")
    
    # RPM-Abfrage in den Hintergrund-Loop schicken
    future = asyncio.run_coroutine_threadsafe(get_rpm_value(), async_loop)
    
    def on_rpm_done(fut):
        try:
            live_rpm = fut.result()
            # UI-Update muss zwingend im Haupt-Thread erfolgen
            root.after(0, lambda: rpm_label.config(text=f"{live_rpm} RPM"))
        except Exception:
            root.after(0, lambda: rpm_label.config(text="--- RPM"))
            
    future.add_done_callback(on_rpm_done)
    
    # Nächste Aktualisierung in 1000ms planen
    root.after(1000, update_live_spin)

async def get_rpm_value():
    status = await driver._get_positions_and_tachometer()
    return int(status.tachometer * -14.6932)

def start_spin_ui():
    global spin_running, spin_start_time
    spin_running = True
    spin_start_time = time.time()
    btn_start.config(text="SPINNING...", bg="dark orange", state="disabled")
    update_live_spin()

def stop_spin_ui():
    global spin_running
    spin_running = False
    btn_start.config(text="START", bg="green", state="normal")
    rpm_label.config(text="--- RPM")



async def connect_task():
    global is_connected
    if not is_connected:
        root.after(0, set_connect_wait_ui)
        try:
            await driver.setup()
            # --- NEU: Nach dem Setup direkt Bucket 1 anfahren und Tür öffnen ---
            print("\n[GUI] Setup abgeschlossen. Richte Bucket 1 für den Start aus...")
            await select_bucket_task(1)
            # -------------------------------------------------------------------
            
            root.after(0, update_status_ui, True)
            btn_connect.config(state="normal")
            
        except Exception as e:
            print(f"[GUI FEHLER] Verbindung fehlgeschlagen: {e}")
            root.after(0, update_status_ui, False)
            btn_connect.config(state="normal")
    else:
        pass


async def select_bucket_task(bucket_num):
    ui_lock()
    await driver.close_door()
    await driver.go_to_bucket(bucket_num)
    await driver.open_door()
    ui_unlock()

async def start_task(speed_rpm):
    ui_lock()
    global total_duration
    total_duration = time_slider.get()
    root.after(0, start_spin_ui)
    await driver.close_door()
    await driver.custom_spin(rpm=speed_rpm, duration=float(total_duration))
    await driver.open_door()
    root.after(0, stop_spin_ui)
    ui_unlock()

def ui_lock():
    btn_b1.config(state="disabled")
    btn_b2.config(state="disabled")

def ui_unlock():
    btn_b1.config(state="normal")
    btn_b2.config(state="normal")

# --- 5. Button-Callbacks ---
def toggle_connect():
    btn_connect.config(state="disabled") # Kurz sperren, um Spam-Klicks zu vermeiden
    if not is_connected:
        asyncio.run_coroutine_threadsafe(connect_task(), async_loop)
    else:
        asyncio.run_coroutine_threadsafe(disconnect_task(), async_loop)

def select_bucket(bucket_num):
    if not is_connected: return
    asyncio.run_coroutine_threadsafe(select_bucket_task(bucket_num), async_loop)

def start_action():
    if not is_connected: return
    current_speed = slider.get()
    timer_label.config(text="00:00")
    asyncio.run_coroutine_threadsafe(start_task(current_speed), async_loop)

def stop_action():
    global timer_running
    if not timer_running and btn_start.cget("text") == "START":
        return 
    btn_start.config(text="ABORTING...", bg="red", state="disabled")
    asyncio.run_coroutine_threadsafe(driver.stop_spin_async(), async_loop)

# --- 6. Layout ---
top = tk.Frame(root, bg="white")
top.pack(side="top", fill="x", padx=20, pady=10)

btn_exit = tk.Button(top, text="✖ EXIT", bg="firebrick", fg="white", font=("Helvetica", 12, "bold"), command=on_exit)
btn_exit.pack(side="left")


# Zeit- und RPM-Anzeige nebeneinander
timer_frame = tk.Frame(top, bg="white")
timer_frame.pack(side="left", expand=True, padx=(112, 40))
timer_label = tk.Label(timer_frame, text="00:00", font=("Helvetica", 32, "bold"), bg="white")
timer_label.pack(side="left")
rpm_label = tk.Label(timer_frame, text="--- RPM", font=("Helvetica", 20, "bold"), bg="white", fg="gray")
rpm_label.pack(side="left", padx=(20,0))

btn_connect = tk.Button(top, text="CONNECT", font=("Helvetica", 12, "bold"), command=toggle_connect)
btn_connect.pack(side="right", padx=10)

status_canvas = tk.Canvas(top, width=30, height=30, bg="white", highlightthickness=0)
status_light = status_canvas.create_oval(5, 5, 25, 25, fill="red", outline="black")
status_canvas.pack(side="right")

bot = tk.Frame(root, bg="white")
bot.pack(side="bottom", fill="x", pady=20, padx=20)

btn_start = tk.Button(bot, text="START", bg="green", fg="white", font=("Helvetica", 30, "bold"), height=2, command=start_action)
btn_start.pack(side="left", expand=True, fill="both", padx=10)

btn_stop = tk.Button(bot, text="STOP", bg="red", fg="white", font=("Helvetica", 30, "bold"), height=2, command=stop_action)
btn_stop.pack(side="right", expand=True, fill="both", padx=10)

mid = tk.Frame(root, bg="white")
mid.pack(side="top", expand=True, fill="both")

bucket_frame = tk.Frame(mid, bg="white")
bucket_frame.pack(pady=15)
btn_b1 = tk.Button(bucket_frame, text="BUCKET 1", font=("Helvetica", 22, "bold"), width=15, height=2, bg="light gray", command=lambda: select_bucket(1))
btn_b1.pack(side="left", padx=10)
btn_b2 = tk.Button(bucket_frame, text="BUCKET 2", font=("Helvetica", 22, "bold"), width=15, height=2, bg="light gray", command=lambda: select_bucket(2))
btn_b2.pack(side="left", padx=10)

slider = tk.Scale(mid, from_=1000, to=2990, resolution=100, orient="horizontal", label="Speed (RPM)", font=("Helvetica", 14), length=500, bg="white", highlightthickness=0)
slider.set(1500)
slider.pack(pady=10)

time_slider = tk.Scale(mid, from_=10, to=120, resolution=5, orient="horizontal", label="Spin Time (s) - will take ~ 50s longer than this value sorry :(", font=("Helvetica", 14), length=500, bg="white", highlightthickness=0)
time_slider.set(40)
time_slider.pack(pady=10)

root.bind("<Escape>", on_exit)

root.mainloop()