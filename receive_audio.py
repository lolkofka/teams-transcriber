"""Run the Mac audio receiver and print the destination to paste on Windows."""
import argparse
import secrets
import socket

from audio_receiver import AudioReceiver
from audio_playback import AudioPlayback


def main():
    import pyaudio
    parser = argparse.ArgumentParser(description='Приём звука Teams с Windows на Mac')
    parser.add_argument('--port', type=int, default=8767)
    parser.add_argument('--path', help='Путь приёма; по умолчанию создаётся уникальный адрес')
    device = parser.add_mutually_exclusive_group()
    device.add_argument('--output', type=int, help='Индекс аудиовыхода, например виртуального устройства')
    device.add_argument('--output-name', help='Имя аудиовыхода, например EShareAudio')
    parser.add_argument('--buffer-ms', type=int, default=160, help='Буфер против сетевых пауз, 40–400 мс')
    parser.add_argument('--list-devices', action='store_true')
    args = parser.parse_args()
    if not 40 <= args.buffer_ms <= 400:
        parser.error('--buffer-ms должен быть от 40 до 400')
    output = receiver = None
    try:
        if args.list_devices:
            audio = pyaudio.PyAudio()
            try:
                for index in range(audio.get_device_count()):
                    device = audio.get_device_info_by_index(index)
                    if device['maxOutputChannels']:
                        print(f"{index}: {device['name']}")
            finally:
                audio.terminate()
            return
        output = AudioPlayback(output_index=args.output, output_name=args.output_name,
                               prefill_ms=args.buffer_ms, backend=pyaudio,
                               on_log=lambda message: print(message, flush=True))
        output.start()
        path = args.path or '/audio/' + secrets.token_urlsafe(24)
        receiver = AudioReceiver(output.write, path, port=args.port,
                                 status_provider=output.status,
                                 on_error=lambda message: print('[receiver] ' + message, flush=True))
        receiver.start()
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
                probe.connect(('192.0.2.1', 9))  # Routing lookup only; no packets are sent.
                host = probe.getsockname()[0]
        except OSError:
            host = '<IP-Mac>'
        print('Вставьте этот адрес в поле «Адрес передачи потока» на Windows:')
        print(f'http://{host}:{receiver.port}{path}', flush=True)
        print('Для остановки нажмите Ctrl+C.', flush=True)
        while receiver.thread.is_alive():
            receiver.thread.join(timeout=.5)
    except KeyboardInterrupt:
        pass
    finally:
        if receiver is not None:
            receiver.close()
        if output is not None:
            output.close()


if __name__ == '__main__':
    main()
