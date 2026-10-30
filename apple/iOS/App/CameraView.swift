// "Take Photo" in a folder: the photo goes to the NAS, not to the Photos library. The
// camera stays open for the next shot; each one uploads in the background.
import ImageIO
import SwiftUI
import UIKit
import UniformTypeIdentifiers

struct CameraView: UIViewControllerRepresentable {
    @EnvironmentObject var model: PhoneModel
    @Environment(\.dismiss) private var dismiss
    let folder: FolderRef

    static var isAvailable: Bool { UIImagePickerController.isSourceTypeAvailable(.camera) }

    func makeUIViewController(context: Context) -> UIImagePickerController {
        let p = UIImagePickerController()
        p.sourceType = .camera
        p.mediaTypes = [UTType.image.identifier]
        p.delegate = context.coordinator
        return p
    }
    func updateUIViewController(_ p: UIImagePickerController, context: Context) {}
    func makeCoordinator() -> Coordinator { Coordinator(self) }

    final class Coordinator: NSObject, UIImagePickerControllerDelegate, UINavigationControllerDelegate {
        let parent: CameraView
        init(_ p: CameraView) { parent = p }

        func imagePickerController(_ picker: UIImagePickerController,
                                   didFinishPickingMediaWithInfo info: [UIImagePickerController.InfoKey: Any]) {
            guard let image = info[.originalImage] as? UIImage else { return }
            let meta = info[.mediaMetadata] as? [String: Any] ?? [:]
            let name = Phone.photoName()
            let url = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString + ".heic")
            Task.detached(priority: .userInitiated) {
                guard CameraView.writeHEIC(image, meta: meta, to: url) else { return }
                await MainActor.run { self.parent.model.upload(file: url, name: name, to: self.parent.folder) }
            }
        }

        func imagePickerControllerDidCancel(_ picker: UIImagePickerController) { parent.dismiss() }
    }

    /// HEIC with the camera's own metadata (EXIF, orientation, location if enabled).
    nonisolated static func writeHEIC(_ image: UIImage, meta: [String: Any], to url: URL) -> Bool {
        guard let cg = image.cgImage,
              let dest = CGImageDestinationCreateWithURL(url as CFURL, UTType.heic.identifier as CFString, 1, nil)
        else { return false }
        var props = meta
        props[kCGImagePropertyOrientation as String] = CGImagePropertyOrientation(image.imageOrientation).rawValue
        props[kCGImageDestinationLossyCompressionQuality as String] = 0.9
        CGImageDestinationAddImage(dest, cg, props as CFDictionary)
        return CGImageDestinationFinalize(dest)
    }
}

extension CGImagePropertyOrientation {
    init(_ o: UIImage.Orientation) {
        switch o {
        case .up: self = .up
        case .down: self = .down
        case .left: self = .left
        case .right: self = .right
        case .upMirrored: self = .upMirrored
        case .downMirrored: self = .downMirrored
        case .leftMirrored: self = .leftMirrored
        case .rightMirrored: self = .rightMirrored
        @unknown default: self = .up
        }
    }
}
