/*
 * WiSense-Twin — Section 14: ESP32 CSI Receiver Firmware (rx_csi_collector)
 *
 * Header definitions for binary framed CSI output, queue structures,
 * and transmitter beacon header parsing.
 */

#ifndef CSI_EXTRACTOR_H
#define CSI_EXTRACTOR_H

#include <stdint.h>
#include <stdbool.h>
#include <stddef.h>

#ifdef __cplusplus
extern "C" {
#endif

/*
 * Binary Framing Protocol Constants:
 * Sync marker: 0xAA, 0x55, 0x57, 0x53 ("\xaa\x55WS")
 * End marker:  0x55, 0xAA
 */
#define CSI_FRAME_MAGIC_0       0xAA
#define CSI_FRAME_MAGIC_1       0x55
#define CSI_FRAME_MAGIC_2       0x57  /* ASCII 'W' */
#define CSI_FRAME_MAGIC_3       0x53  /* ASCII 'S' */

#define CSI_FRAME_TRAILER_0     0x55
#define CSI_FRAME_TRAILER_1     0xAA

/*
 * Maximum raw CSI payload buffer size.
 * In ESP-IDF:
 * - HT20: 64 subcarriers * 2 (I/Q) = 128 bytes
 * - HT40: 64 subcarriers * 2 (I/Q) * 3 = 384 bytes
 */
#define CSI_MAX_DATA_LEN        384

/*
 * Expected transmitter beacon header layout (emitted by tx_beacon in Section 13).
 * Resides at byte offset 15 in the ESP-NOW payload.
 */
#pragma pack(push, 1)
typedef struct {
    uint32_t seq_num;       /**< Monotonic packet sequence counter (bytes 0-3) */
    uint32_t timestamp_ms;  /**< Sender timestamp in milliseconds since boot */
    uint16_t packet_rate;   /**< Target transmission rate in Hz */
    uint8_t  channel;       /**< Configured WiFi channel */
    uint8_t  magic;         /**< Diagnostic identifier: 'W' (0x57) */
} tx_beacon_header_t;

/**
 * @brief Binary frame fixed header (28 bytes).
 *
 * Precedes raw CSI byte array in UART output stream.
 */
typedef struct {
    uint8_t  magic[4];            /**< Framing sync marker: {0xAA, 0x55, 0x57, 0x53} */
    uint16_t header_len;          /**< Size of this header in bytes (28) */
    uint16_t payload_len;         /**< Length of following raw CSI byte array (csi_len) */
    uint32_t seq_num;             /**< Transmitter sequence number (or rx fallback counter) */
    uint32_t rx_timestamp_us;     /**< Receiver local microsecond timestamp (esp_timer_get_time) */
    uint32_t sender_timestamp_ms; /**< Sender millisecond timestamp (from beacon payload) */
    int8_t   rssi;                /**< Received Signal Strength Indicator (dBm) */
    uint8_t  channel;             /**< Primary channel on which frame was received */
    int8_t   noise_floor;         /**< RF noise floor (dBm) */
    uint8_t  first_word_invalid;  /**< Hardware erratum flag: 1 if first word is invalid */
    int8_t   sig_mode;            /**< Signal mode: 0: non-HT, 1: HT20, 2: HT40 */
    int8_t   mcs;                 /**< Modulation and Coding Scheme index */
    uint8_t  reserved[2];         /**< Reserved padding for 32-bit boundary alignment */
} csi_frame_header_t;

/**
 * @brief Binary frame trailer (4 bytes).
 *
 * Appended immediately after raw CSI byte array.
 */
typedef struct {
    uint16_t crc16;               /**< CRC-16-CCITT (poly 0x1021, init 0xFFFF) over header + payload */
    uint8_t  trailer[2];          /**< Framing end marker: {0x55, 0xAA} */
} csi_frame_trailer_t;
#pragma pack(pop)

/**
 * @brief In-memory record structure passed through the FreeRTOS queue.
 */
typedef struct {
    csi_frame_header_t header;
    uint8_t            csi_data[CSI_MAX_DATA_LEN];
} csi_record_t;

/**
 * @brief Extract transmitter sequence number and timestamp from beacon payload if present.
 *
 * @param payload Pointer to 802.11 frame payload buffer from wifi_csi_info_t
 * @param payload_len Length of payload buffer
 * @param out_seq Output pointer for sequence number
 * @param out_sender_ts Output pointer for sender timestamp in ms
 * @return true if valid tx_beacon magic was verified, false otherwise
 */
static inline bool csi_extract_beacon_meta(const uint8_t *payload, uint16_t payload_len,
                                          uint32_t *out_seq, uint32_t *out_sender_ts)
{
    if (payload != NULL && payload_len >= (15 + sizeof(tx_beacon_header_t))) {
        const tx_beacon_header_t *beacon = (const tx_beacon_header_t *)(payload + 15);
        if (beacon->magic == 0x57) { // 'W'
            if (out_seq != NULL) {
                *out_seq = beacon->seq_num;
            }
            if (out_sender_ts != NULL) {
                *out_sender_ts = beacon->timestamp_ms;
            }
            return true;
        }
    }
    return false;
}

/**
 * @brief Compute standard CRC-16-CCITT (polynomial 0x1021, initial value 0xFFFF).
 *
 * Matches Python's `binascii.crc_hqx(data, 0xFFFF)` exactly.
 *
 * @param header Pointer to packed frame header
 * @param payload Pointer to raw CSI byte array
 * @param payload_len Length of raw CSI byte array in bytes
 * @return Calculated 16-bit CRC checksum
 */
static inline uint16_t csi_compute_crc16(const csi_frame_header_t *header,
                                        const uint8_t *payload,
                                        uint16_t payload_len)
{
    uint16_t crc = 0xFFFF;
    const uint8_t *h_bytes = (const uint8_t *)header;

    for (size_t i = 0; i < sizeof(csi_frame_header_t); i++) {
        crc ^= (uint16_t)h_bytes[i] << 8;
        for (int j = 0; j < 8; j++) {
            if (crc & 0x8000) {
                crc = (crc << 1) ^ 0x1021;
            } else {
                crc <<= 1;
            }
        }
    }

    if (payload != NULL && payload_len > 0) {
        for (size_t i = 0; i < payload_len; i++) {
            crc ^= (uint16_t)payload[i] << 8;
            for (int j = 0; j < 8; j++) {
                if (crc & 0x8000) {
                    crc = (crc << 1) ^ 0x1021;
                } else {
                    crc <<= 1;
                }
            }
        }
    }

    return crc;
}

#ifdef __cplusplus
}
#endif

#endif // CSI_EXTRACTOR_H
