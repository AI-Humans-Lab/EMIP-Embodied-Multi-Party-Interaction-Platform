import queue
import threading
from RealtimeSTT import AudioToTextRecorder
from .utils import make_file_logger

class RealtimeSTT:
    def __init__(self, 
                 logdir,
                 mic_device:int = 1,
                 unknown_sentence_detection_pause:float = 0.7
                 ):
        """日本語の音声認識器と、PCM音声の入力キュー・認識文字列の出力キューを用意する。

        マイク入力ではなく、渡された音声を認識する設定にする。
        """
        self.logdir = logdir
        self.input_buffer = queue.Queue()
        self.output_buffer = queue.Queue()
        self.mic_device = mic_device
        self.unknown_sentence_detection_pause = unknown_sentence_detection_pause
        self.logger = make_file_logger(f"asr", f"{logdir}/asr.log")

        recorder_config = {
        'use_microphone':False,
        'spinner': True,
        'device': 'cuda',
        #'model': 'large-v2', # or large-v2 or deepdml/faster-whisper-large-v3-turbo-ct2 or ...
        'model': 'turbo', # or large-v2 or deepdml/faster-whisper-large-v3-turbo-ct2 or ...
        # 'download_root': None, # default download root location. Ex. ~/.cache/huggingface/hub/ in Linux
        'input_device_index': mic_device,
        'realtime_model_type': 'turbo', # or small.en or distil-small.en or ...
        'language': 'ja',
         'silero_sensitivity': 0.05,
         'webrtc_sensitivity': 3,
         'post_speech_silence_duration': unknown_sentence_detection_pause,
        'min_length_of_recording': 1.1,        
        'min_gap_between_recordings': 0,                
        'enable_realtime_transcription': True,
        # 'realtime_processing_pause': 0.02,
        # 'realtime_processing_pause': 0.1,
        'realtime_processing_pause': 0.2,
        'on_realtime_transcription_update': self.realtime_transcription_update,
        # 'on_realtime_transcription_stabilized': text_detected,
        'silero_deactivity_detection': True,
        'early_transcription_on_silence': 0,
        'beam_size': 10,
        # 'beam_size': 10,
        'beam_size_realtime': 10,
        # 'beam_size_realtime': 10,
         'batch_size': 0,
         'realtime_batch_size': 0,        
         'no_log_file': False,
         'initial_prompt_realtime': (
             "End incomplete sentences with ellipses.\n"
             "Examples:\n"
             "Complete: The sky is blue.\n"
             "Incomplete: When the sky...\n"
             "Complete: She walked home.\n"
             "Incomplete: Because he...\n"
         ),
         'silero_use_onnx': True,
         'faster_whisper_vad_filter': False,
        }
        # end_of_sentence_detection_pause = 0.45
        # mid_sentence_detection_pause = 2.0

        self.recorder = AudioToTextRecorder(**recorder_config)
        print(f"finish create realtimeSTT instance")

    def realtime_transcription_update(self, realtime_recognized):
        """認識途中の文字列をログへ記録し、未確定の結果として出力キューに追加する。"""
        self.logger.info('------ in realtime transcriptin update ---------')
        self.logger.info(f"partial utterance: {realtime_recognized}")
        self.output_buffer.put((realtime_recognized, False))
        self.logger.info('------  out realtime transcriptin update ---------\n')

    def transcription_after_pause(self, recognized):
        """発話の区切りで確定した文字列をログへ記録し、確定結果として出力キューに追加する。"""
        self.logger.info('------ in transcription after pausee ---------')
        self.logger.info(f"final utterane: {recognized}")
        self.output_buffer.put((recognized, True))
        self.logger.info('------  out transcription after pause  ---------\n')

    def audio_feed_loop(self):
        """入力キューからPCM音声を繰り返し取り出し、音声認識器へ渡す。"""
        while True:
            try:
                pcm_bytes = self.input_buffer.get(timeout=0.1)
            except queue.Empty:
                continue
        
            self.recorder.feed_audio(pcm_bytes)

    def transcription_loop(self):
        """音声認識器から確定した発話を受け取り、確定結果の通知処理へ渡す処理を繰り返す。"""
        while True:
            self.recorder.text(self.transcription_after_pause)

    def run(self):
        """音声の供給と文字起こしを、それぞれバックグラウンドのスレッドで開始する。"""
        self.logger.info("ASR start")
        get_audio_thread = threading.Thread(target=self.audio_feed_loop, daemon=True)
        get_audio_thread.start()
        transcription_thread = threading.Thread(target=self.transcription_loop, daemon=True)
        transcription_thread.start()